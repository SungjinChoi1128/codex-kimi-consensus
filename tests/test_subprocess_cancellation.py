from __future__ import annotations

import sys
import threading
import time
import os

import pytest

from consensusd.db import Database
from consensusd.mcp_server import ConsensusService
from consensusd.models import RunStatus
from consensusd.orchestrator import Orchestrator
from consensusd.runners.base import RunnerCancelled, reset_cancel_check, set_cancel_check
from consensusd.runners.subprocess_utils import run_cancellable_command
from consensusd.settings import Settings


def test_cancellable_subprocess_terminates_process_group(tmp_path):
    started = tmp_path / "started.txt"
    terminated = tmp_path / "terminated.txt"
    script = tmp_path / "sleeping_agent.py"
    script.write_text(
        "\n".join(
            [
                "import signal",
                "import os",
                "import sys",
                "import time",
                "from pathlib import Path",
                f"started = Path({str(started)!r})",
                f"terminated = Path({str(terminated)!r})",
                "def handle_term(signum, frame):",
                "    terminated.write_text('terminated')",
                "    sys.exit(0)",
                "signal.signal(signal.SIGTERM, handle_term)",
                "started.write_text('started')",
                "while True:",
                "    time.sleep(0.1)",
            ]
        )
    )

    token = set_cancel_check(lambda: started.exists())
    try:
        with pytest.raises(RunnerCancelled):
            run_cancellable_command(
                [sys.executable, str(script)],
                cwd=tmp_path,
                timeout_sec=10,
                label="test agent",
                poll_interval=0.05,
            )
    finally:
        reset_cancel_check(token)

    assert terminated.read_text() == "terminated"


def test_orchestrator_cancellation_kills_active_codex_subprocess(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    started = tmp_path / "started.txt"
    pid_file = tmp_path / "pid.txt"
    terminated = tmp_path / "terminated.txt"
    script = tmp_path / "sleeping_codex.py"
    script.write_text(
        "\n".join(
            [
                "import signal",
                "import os",
                "import sys",
                "import time",
                "from pathlib import Path",
                f"started = Path({str(started)!r})",
                f"pid_file = Path({str(pid_file)!r})",
                f"terminated = Path({str(terminated)!r})",
                "def handle_term(signum, frame):",
                "    terminated.write_text('terminated')",
                "    sys.exit(0)",
                "signal.signal(signal.SIGTERM, handle_term)",
                "pid_file.write_text(str(os.getpid()))",
                "started.write_text('started')",
                "while True:",
                "    time.sleep(0.1)",
            ]
        )
    )
    settings = Settings(
        db_path=tmp_path / "consensus.sqlite",
        project_root=project_root,
        codex_command=(sys.executable, str(script)),
        subprocess_timeout_sec=10,
        heartbeat_interval_sec=0.05,
        runner_mode="codex",
    )
    db = Database(settings.db_path)
    db.init()
    orchestrator = Orchestrator(db, settings)
    service = ConsensusService(db, settings, orchestrator=None)
    run = db.create_run("cancel active codex subprocess", str(project_root), "approval-gated", runner_mode="codex")
    run = db.transition_run(run.run_id, RunStatus.AWAITING_CODEX_PROPOSAL, expected_version=run.version)
    db.transition_run(run.run_id, RunStatus.CODEX_DRAFTING, expected_version=run.version)

    thread = threading.Thread(target=orchestrator.tick)
    thread.start()
    deadline = time.monotonic() + 5
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert started.exists()

    service.tool_cancel_consensus_review(run.run_id)
    thread.join(timeout=5)

    assert not thread.is_alive()
    transcript = db.transcript(run.run_id)
    assert transcript.run.status == RunStatus.CANCELLED
    assert transcript.proposals == []
    assert wait_for_pid_exit(int(pid_file.read_text()), timeout=5)
    if terminated.exists():
        assert terminated.read_text() == "terminated"
    transcript = db.transcript(run.run_id)
    assert any(event.event_type == "run.cancel_requested" for event in transcript.events)


def test_attached_lease_expiry_kills_active_codex_subprocess(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    started = tmp_path / "lease_started.txt"
    pid_file = tmp_path / "lease_pid.txt"
    terminated = tmp_path / "lease_terminated.txt"
    script = tmp_path / "sleeping_codex.py"
    script.write_text(
        "\n".join(
            [
                "import signal",
                "import os",
                "import sys",
                "import time",
                "from pathlib import Path",
                f"started = Path({str(started)!r})",
                f"pid_file = Path({str(pid_file)!r})",
                f"terminated = Path({str(terminated)!r})",
                "def handle_term(signum, frame):",
                "    terminated.write_text('terminated')",
                "    sys.exit(0)",
                "signal.signal(signal.SIGTERM, handle_term)",
                "pid_file.write_text(str(os.getpid()))",
                "started.write_text('started')",
                "while True:",
                "    time.sleep(0.1)",
            ]
        )
    )
    settings = Settings(
        db_path=tmp_path / "lease.sqlite",
        project_root=project_root,
        codex_command=(sys.executable, str(script)),
        subprocess_timeout_sec=10,
        heartbeat_interval_sec=0.05,
        runner_mode="codex",
    )
    db = Database(settings.db_path)
    db.init()
    orchestrator = Orchestrator(db, settings)
    run = db.create_run(
        "cancel when attached chat disappears",
        str(project_root),
        "approval-gated",
        runner_mode="codex",
        lease_mode="attached",
        lease_ttl_seconds=1,
    )
    run = db.transition_run(run.run_id, RunStatus.AWAITING_CODEX_PROPOSAL, expected_version=run.version)
    db.transition_run(run.run_id, RunStatus.CODEX_DRAFTING, expected_version=run.version)

    thread = threading.Thread(target=orchestrator.tick)
    thread.start()
    deadline = time.monotonic() + 5
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert started.exists()

    thread.join(timeout=5)

    assert not thread.is_alive()
    transcript = db.transcript(run.run_id)
    assert transcript.run.status == RunStatus.CANCELLED
    assert transcript.run.error == "attached client heartbeat expired"
    assert wait_for_pid_exit(int(pid_file.read_text()), timeout=5)
    if terminated.exists():
        assert terminated.read_text() == "terminated"
    transcript = db.transcript(run.run_id)
    assert any(event.event_type == "run.lease_expired" for event in transcript.events)
    assert any(event.event_type == "codex.proposal.cancelled" for event in transcript.events)


def wait_for_pid_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_is_running(pid):
            return True
        time.sleep(0.05)
    return not pid_is_running(pid)


def pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True

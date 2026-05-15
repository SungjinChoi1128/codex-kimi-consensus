from __future__ import annotations

import sys

import pytest

from consensusd.runners.base import RunnerCancelled, reset_cancel_check, set_cancel_check
from consensusd.runners.subprocess_utils import run_cancellable_command


def test_cancellable_subprocess_terminates_process_group(tmp_path):
    started = tmp_path / "started.txt"
    terminated = tmp_path / "terminated.txt"
    script = tmp_path / "sleeping_agent.py"
    script.write_text(
        "\n".join(
            [
                "import signal",
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

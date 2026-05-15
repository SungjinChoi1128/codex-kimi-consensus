from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.request import urlopen

import uvicorn

from .db import Database
from .mcp_server import ConsensusService, create_app
from .models import RunStatus
from .orchestrator import Orchestrator
from .security import require_localhost
from .settings import Settings

try:
    import typer
except ModuleNotFoundError:  # pragma: no cover - exercised by local smoke check
    typer = None


DEFAULT_DB = Path(".consensusd/consensusd.sqlite")
PID_DIR = Path(".consensusd")


def _settings(
    db: Optional[Path],
    project_root: Optional[Path],
    host: str = "127.0.0.1",
    port: int = 8787,
    dev_auth_role: Optional[str] = None,
    runner_mode: Optional[str] = None,
) -> Settings:
    return Settings.from_env(
        db_path=db,
        project_root=project_root,
        host=host,
        port=port,
        dev_auth_role=dev_auth_role,
        runner_mode=runner_mode,
    )


def _service(db_path: Optional[Path], project_root: Optional[Path]) -> tuple[ConsensusService, Orchestrator]:
    settings = _settings(db_path, project_root)
    db = Database(settings.db_path)
    db.init()
    orchestrator = Orchestrator(db, settings)
    return ConsensusService(db, settings, orchestrator), orchestrator


def _pid_path(db: Path, port: int) -> Path:
    return db.parent / f"consensusd-{port}.pid"


def _log_path(db: Path, port: int) -> Path:
    return db.parent / "logs" / f"consensusd-{port}.log"


def _is_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_pid(path: Path) -> Optional[int]:
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _healthz(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with urlopen(f"http://{host}:{port}/healthz", timeout=timeout) as response:
            return response.status == 200
    except (OSError, TimeoutError, URLError):
        return False


def _print_json(data: object) -> None:
    print(json.dumps(data, indent=2))


def _print_status(data: dict[str, object]) -> None:
    print(f"Run: {data['run_id']}")
    print(f"Status: {data['status']}  round={data['current_round']}  version={data['version']}")
    print(f"Mode: {data['mode']}  runner={data.get('runner_mode', 'unknown')}")
    print(f"Project: {data['project_root']}")
    print(f"Objective: {data['objective']}")
    if data.get("locked_at"):
        print(f"Consensus locked: {data['locked_at']}")
    if data.get("error"):
        print(f"Error: {data['error']}")
    if data.get("note"):
        print(f"Note: {data['note']}")


def _print_brief(data: dict[str, object]) -> None:
    print(f"Run: {data['run_id']}")
    print(f"Status: {data['status']}  phase={data['current_phase']}  round={data['round']}/{data['max_rounds']}")
    print(f"Runner: {data['runner_mode']}")
    if data.get("error"):
        print(f"Error: {data['error']}")
    print(f"Next: {data['next_action']}")
    if data.get("last_kimi_status"):
        print(f"\nKimi: {data['last_kimi_status']}")
        print(_clip(str(data.get("last_kimi_summary") or ""), 900))
    if data.get("revision_changed_files"):
        print("\nCodex revision changed files")
        for path in data["revision_changed_files"]:
            print(f"- {path}")
    if data.get("guardrail_violations"):
        print("\nGuardrail violations")
        for path in data["guardrail_violations"]:
            print(f"- {path}")
    if data.get("omx_plan_path"):
        print(f"\nOMX plan: {data['omx_plan_path']}")
    if data.get("context_bridge_path"):
        print(f"Context bridge: {data['context_bridge_path']}")
    events = data.get("phase_events") or []
    if events:
        print("\nRecent phases")
        for event in events[-5:]:
            payload = event.get("payload", {})
            elapsed = f" elapsed={payload.get('elapsed_seconds')}s" if payload.get("elapsed_seconds") is not None else ""
            print(f"- {event['event_type']} round={payload.get('round')}{elapsed}")


def _progress_line(data: dict[str, object]) -> str:
    event = data.get("latest_phase_event") or {}
    event_type = event.get("event_type") if isinstance(event, dict) else None
    payload = event.get("payload", {}) if isinstance(event, dict) else {}
    heartbeat = payload.get("heartbeat") if isinstance(payload, dict) and str(event_type or "").endswith(".heartbeat") else None
    suffix = f" heartbeat={heartbeat}" if heartbeat is not None else ""
    phase = data["current_phase"]
    return f"{data['status']}  phase={phase}  round={data['round']}/{data['max_rounds']}{suffix}"


def _print_negotiation_summary(data: dict[str, object]) -> None:
    proposals = data.get("proposals", [])
    reviews = data.get("reviews", [])
    if not isinstance(proposals, list) or not isinstance(reviews, list):
        return
    if not proposals and not reviews:
        return
    print("\nNegotiation")
    for proposal in proposals:
        if not isinstance(proposal, dict):
            continue
        round_no = proposal.get("round")
        runner = proposal.get("runner", "unknown")
        print(f"- Round {round_no}: Codex proposal ({runner})")
        matching = [review for review in reviews if isinstance(review, dict) and review.get("round") == round_no]
        for review in matching:
            status = review.get("status")
            review_runner = review.get("runner", "unknown")
            summary = _clip(" ".join(str(review.get("content", "")).split()), 500)
            print(f"  Kimi {status} ({review_runner}): {summary}")


def _clip(text: str, limit: int = 1800) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n... truncated {len(text) - limit} chars; rerun with --json for full transcript"


def _print_transcript(data: dict[str, object]) -> None:
    run = data["run"]
    assert isinstance(run, dict)
    _print_status(run)
    print()
    print("Transcript")
    print(f"- proposals: {len(data['proposals'])}")
    print(f"- reviews: {len(data['reviews'])}")
    print(f"- evidence: {len(data['evidence'])}")
    print(f"- omx_plans: {len(data['omx_plans'])}")
    print(f"- context_bridges: {len(data.get('context_bridges', []))}")
    print(f"- events: {len(data['events'])}")

    evidence = data["evidence"]
    if isinstance(evidence, list) and evidence:
        print("\nEvidence")
        for item in evidence:
            print(f"[round {item['round']}] {item['kind']} {item['status']} {item.get('command') or ''}".rstrip())
            print(_clip(item["output"], 1200))

    proposals = data["proposals"]
    reviews = data["reviews"]
    if isinstance(proposals, list):
        for proposal in proposals:
            print(f"\nCodex proposal round {proposal['round']} ({proposal.get('runner', 'unknown')})")
            print(_clip(proposal["content"]))
            matching = [
                review for review in reviews if isinstance(review, dict) and review["round"] == proposal["round"]
            ]
            for review in matching:
                print(f"\nKimi review round {review['round']} ({review.get('runner', 'unknown')}): {review['status']}")
                print(_clip(review["content"]))

    plans = data["omx_plans"]
    if isinstance(plans, list) and plans:
        print(f"\nLatest OMX plan ({plans[-1].get('runner', 'unknown')})")
        if plans[-1].get("path"):
            print(f"Path: {plans[-1]['path']}")
        print(_clip(plans[-1]["content"], 2400))

    bridges = data.get("context_bridges", [])
    if isinstance(bridges, list) and bridges:
        print("\nContext bridge")
        print(f"Path: {bridges[-1]['path']}")
        print(_clip(bridges[-1]["content"], 2400))

    handoffs = data["ralph_handoffs"]
    if isinstance(handoffs, list) and handoffs:
        print("\nRalph handoffs")
        for handoff in handoffs:
            print(f"- {handoff['status']} {handoff['created_at']} {handoff['handoff_id']}")


def init_command(db: Path = DEFAULT_DB) -> None:
    Database(db).init()
    print(f"Initialized {db}")


def start_command(
    project_root: Path = Path("."),
    db: Path = DEFAULT_DB,
    host: str = "127.0.0.1",
    port: int = 8787,
    dev_auth_role: Optional[str] = None,
    runner_mode: Optional[str] = None,
) -> None:
    require_localhost(host)
    settings = _settings(db, project_root, host, port, dev_auth_role=dev_auth_role, runner_mode=runner_mode)
    Database(settings.db_path).init()
    print(
        f"Starting consensusd on http://{settings.host}:{settings.port} "
        f"with db {settings.db_path} runner={settings.runner_mode}"
    )
    if settings.dev_auth_role:
        print(f"WARNING: dev no-token MCP role enabled: {settings.dev_auth_role}")
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


def _spawn_endpoint(
    project_root: Path,
    db: Path,
    host: str,
    port: int,
    dev_auth_role: str,
    runner_mode: str,
) -> dict[str, object]:
    require_localhost(host)
    db = Path(db)
    db.parent.mkdir(parents=True, exist_ok=True)
    (db.parent / "logs").mkdir(parents=True, exist_ok=True)
    pid_file = _pid_path(db, port)
    existing = _read_pid(pid_file)
    if existing and _is_pid_running(existing):
        return {"port": port, "role": dev_auth_role, "pid": existing, "status": "already-running", "log": str(_log_path(db, port))}

    log_file = _log_path(db, port)
    command = [
        sys.executable,
        "-m",
        "consensusd",
        "start",
        "--project-root",
        str(project_root),
        "--db",
        str(db),
        "--host",
        host,
        "--port",
        str(port),
        "--dev-auth-role",
        dev_auth_role,
        "--runner-mode",
        runner_mode,
    ]
    with log_file.open("ab") as log:
        process = subprocess.Popen(
            command,
            cwd=Path.cwd(),
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    pid_file.write_text(str(process.pid))
    deadline = time.time() + 5
    while time.time() < deadline:
        if _healthz(host, port):
            return {"port": port, "role": dev_auth_role, "pid": process.pid, "status": "started", "log": str(log_file)}
        if process.poll() is not None:
            return {"port": port, "role": dev_auth_role, "pid": process.pid, "status": "failed", "log": str(log_file)}
        time.sleep(0.1)
    return {"port": port, "role": dev_auth_role, "pid": process.pid, "status": "starting", "log": str(log_file)}


def up_command(
    project_root: Path = Path("."),
    db: Path = DEFAULT_DB,
    host: str = "127.0.0.1",
    port: int = 8787,
    kimi_port: int = 8788,
    with_kimi: bool = True,
    runner_mode: str = "mock",
) -> None:
    """Start localhost MCP endpoints in the background."""
    Database(db).init()
    results = [_spawn_endpoint(project_root.resolve(), db, host, port, "control_surface", runner_mode)]
    if with_kimi:
        results.append(_spawn_endpoint(project_root.resolve(), db, host, kimi_port, "kimi_reviewer", runner_mode))
    print("consensusd is available in the background")
    for result in results:
        print(
            f"- {result['role']} http://{host}:{result['port']}/mcp "
            f"{result['status']} pid={result['pid']} log={result['log']}"
        )
    print(f"db: {db}")


def down_command(db: Path = DEFAULT_DB, ports: Optional[list[int]] = None) -> None:
    ports = ports or [8787, 8788]
    for port in ports:
        pid_file = _pid_path(db, port)
        pid = _read_pid(pid_file)
        if not pid:
            print(f"- port {port}: no pid file")
            continue
        if _is_pid_running(pid):
            os.kill(pid, signal.SIGTERM)
            print(f"- port {port}: stopped pid={pid}")
        else:
            print(f"- port {port}: stale pid={pid}")
        pid_file.unlink(missing_ok=True)


def status_command(run_id: str, db: Path = DEFAULT_DB, json_output: bool = False) -> None:
    service, _ = _service(db, None)
    data = service.tool_get_consensus_status(run_id)
    _print_json(data) if json_output else _print_status(data)


def brief_command(run_id: str, db: Path = DEFAULT_DB, json_output: bool = False) -> None:
    service, _ = _service(db, None)
    data = service.tool_get_consensus_brief(run_id)
    _print_json(data) if json_output else _print_brief(data)


def watch_command(run_id: str, interval: float = 5.0, db: Path = DEFAULT_DB) -> None:
    service, _ = _service(db, None)
    last_line = None
    while True:
        brief = service.tool_get_consensus_brief(run_id)
        run = service.db.get_run(run_id)
        line = f"{run.updated_at} {run.run_id} {_progress_line(brief)}"
        if line != last_line:
            print(line)
            last_line = line
        if run.status in {
            RunStatus.AWAITING_HUMAN_APPROVAL,
            RunStatus.RALPH_HANDOFF_COMPLETE,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            break
        time.sleep(interval)


def review_command(
    objective: str,
    project_root: Path = Path("."),
    db: Path = DEFAULT_DB,
    mode: str = "approval-gated",
    max_rounds: int = 5,
    interval: float = 0.25,
    json_output: bool = False,
    runner_mode: str = "mock",
    show_transcript: bool = False,
) -> None:
    """Run the whole approval-gated review loop in this process."""
    settings = _settings(db, project_root, runner_mode=runner_mode)
    database = Database(settings.db_path)
    database.init()
    orchestrator = Orchestrator(database, settings)
    service = ConsensusService(database, settings, orchestrator=None)
    last_progress = None
    with orchestrator.exclusive():
        result = service.tool_start_consensus_review(objective, str(project_root.resolve()), mode, max_rounds)
        run_id = result["run_id"]
        if not json_output:
            print(f"Started consensus review: {run_id}")
            print(f"Project: {project_root.resolve()}")
            print(f"Runner: {result.get('runner_mode', 'unknown')}")
            if result.get("note"):
                print(f"Note: {result['note']}")
            print()
        while True:
            orchestrator._tick_unlocked()
            run = service.db.get_run(run_id)
            if not json_output:
                brief = service.tool_get_consensus_brief(run_id)
                progress = _progress_line(brief)
                if progress != last_progress:
                    print(progress)
                    last_progress = progress
            if run.status in {
                RunStatus.AWAITING_HUMAN_APPROVAL,
                RunStatus.RALPH_HANDOFF_COMPLETE,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            }:
                break
            time.sleep(interval)

    brief = service.tool_get_consensus_brief(run_id)
    transcript = service.tool_get_consensus_transcript(run_id)
    if json_output:
        payload = {"run_id": run_id, "brief": brief}
        if show_transcript:
            payload["transcript"] = transcript
        _print_json(payload)
        return
    print()
    _print_brief(brief)
    _print_negotiation_summary(transcript)
    if show_transcript:
        print()
        _print_transcript(transcript)
    else:
        print("\nFull transcript: consensusd transcript " f"{run_id} --db {settings.db_path}")


def transcript_command(run_id: str, db: Path = DEFAULT_DB, json_output: bool = False) -> None:
    service, _ = _service(db, None)
    data = service.tool_get_consensus_transcript(run_id)
    _print_json(data) if json_output else _print_transcript(data)


def approve_command(run_id: str, db: Path = DEFAULT_DB, json_output: bool = False) -> None:
    service, orchestrator = _service(db, None)
    service.tool_approve_ralph_handoff(run_id)
    orchestrator.tick_until_idle()
    data = service.tool_get_consensus_status(run_id)
    _print_json(data) if json_output else _print_status(data)


def cancel_command(run_id: str, db: Path = DEFAULT_DB) -> None:
    service, _ = _service(db, None)
    print(json.dumps(service.tool_cancel_consensus_review(run_id), indent=2))


if typer is not None:
    app = typer.Typer(help="consensusd local consensus review daemon")

    @app.command("init")
    def typer_init(db: Path = typer.Option(DEFAULT_DB, "--db")) -> None:
        """Initialize the SQLite database."""
        init_command(db)

    @app.command("start")
    def typer_start(
        project_root: Path = typer.Option(Path("."), "--project-root"),
        db: Path = typer.Option(DEFAULT_DB, "--db"),
        host: str = typer.Option("127.0.0.1", "--host"),
        port: int = typer.Option(8787, "--port"),
        dev_auth_role: Optional[str] = typer.Option(None, "--dev-auth-role"),
        runner_mode: str = typer.Option("mock", "--runner-mode", help="mock, codex, codex-kimi, or codex-kimi-edit"),
    ) -> None:
        """Start the localhost daemon."""
        start_command(project_root, db, host, port, dev_auth_role, runner_mode)

    @app.command("up")
    def typer_up(
        project_root: Path = typer.Option(Path("."), "--project-root"),
        db: Path = typer.Option(DEFAULT_DB, "--db"),
        host: str = typer.Option("127.0.0.1", "--host"),
        port: int = typer.Option(8787, "--port"),
        kimi_port: int = typer.Option(8788, "--kimi-port"),
        with_kimi: bool = typer.Option(True, "--with-kimi/--no-kimi"),
        runner_mode: str = typer.Option("mock", "--runner-mode", help="mock, codex, codex-kimi, or codex-kimi-edit"),
    ) -> None:
        """Start background localhost MCP endpoints; no extra terminal needed."""
        up_command(project_root, db, host, port, kimi_port, with_kimi, runner_mode)

    @app.command("down")
    def typer_down(
        db: Path = typer.Option(DEFAULT_DB, "--db"),
        ports: Optional[list[int]] = typer.Option(None, "--port"),
    ) -> None:
        """Stop background endpoints started by `consensusd up`."""
        down_command(db, ports)

    @app.command("review")
    def typer_review(
        objective: str = typer.Argument(..., help="Review objective."),
        project_root: Path = typer.Option(Path("."), "--project-root"),
        db: Path = typer.Option(DEFAULT_DB, "--db"),
        mode: str = typer.Option("approval-gated", "--mode"),
        max_rounds: int = typer.Option(5, "--max-rounds"),
        interval: float = typer.Option(0.25, "--interval"),
        json_output: bool = typer.Option(False, "--json", help="Print raw JSON."),
        runner_mode: str = typer.Option("mock", "--runner-mode", help="mock, codex, codex-kimi, or codex-kimi-edit"),
        show_transcript: bool = typer.Option(False, "--show-transcript", help="Print the full transcript after the brief."),
    ) -> None:
        """Start, watch, and print an approval-gated review in one Codex-friendly command."""
        review_command(objective, project_root, db, mode, max_rounds, interval, json_output, runner_mode, show_transcript)

    @app.command("status")
    def typer_status(
        run_id: str,
        db: Path = typer.Option(DEFAULT_DB, "--db"),
        json_output: bool = typer.Option(False, "--json", help="Print raw JSON."),
    ) -> None:
        status_command(run_id, db, json_output)

    @app.command("brief")
    def typer_brief(
        run_id: str,
        db: Path = typer.Option(DEFAULT_DB, "--db"),
        json_output: bool = typer.Option(False, "--json", help="Print raw JSON."),
    ) -> None:
        """Print a Codex-friendly progress brief."""
        brief_command(run_id, db, json_output)

    @app.command("watch")
    def typer_watch(
        run_id: str,
        interval: float = typer.Option(5.0, "--interval"),
        db: Path = typer.Option(DEFAULT_DB, "--db"),
    ) -> None:
        watch_command(run_id, interval, db)

    @app.command("transcript")
    def typer_transcript(
        run_id: str,
        db: Path = typer.Option(DEFAULT_DB, "--db"),
        json_output: bool = typer.Option(False, "--json", help="Print raw JSON."),
    ) -> None:
        transcript_command(run_id, db, json_output)

    @app.command("approve")
    def typer_approve(
        run_id: str,
        db: Path = typer.Option(DEFAULT_DB, "--db"),
        json_output: bool = typer.Option(False, "--json", help="Print raw JSON."),
    ) -> None:
        approve_command(run_id, db, json_output)

    @app.command("cancel")
    def typer_cancel(run_id: str, db: Path = typer.Option(DEFAULT_DB, "--db")) -> None:
        cancel_command(run_id, db)
else:

    def app() -> None:
        parser = argparse.ArgumentParser(prog="consensusd", description="consensusd local consensus review daemon")
        sub = parser.add_subparsers(dest="command", required=True)

        init_p = sub.add_parser("init")
        init_p.add_argument("--db", type=Path, default=DEFAULT_DB)

        start_p = sub.add_parser("start")
        start_p.add_argument("--project-root", type=Path, default=Path("."))
        start_p.add_argument("--db", type=Path, default=DEFAULT_DB)
        start_p.add_argument("--host", default="127.0.0.1")
        start_p.add_argument("--port", type=int, default=8787)
        start_p.add_argument("--dev-auth-role", default=None)
        start_p.add_argument("--runner-mode", default="mock")

        up_p = sub.add_parser("up")
        up_p.add_argument("--project-root", type=Path, default=Path("."))
        up_p.add_argument("--db", type=Path, default=DEFAULT_DB)
        up_p.add_argument("--host", default="127.0.0.1")
        up_p.add_argument("--port", type=int, default=8787)
        up_p.add_argument("--kimi-port", type=int, default=8788)
        up_p.add_argument("--no-kimi", action="store_true")
        up_p.add_argument("--runner-mode", default="mock")

        down_p = sub.add_parser("down")
        down_p.add_argument("--db", type=Path, default=DEFAULT_DB)
        down_p.add_argument("--port", action="append", type=int)

        review_p = sub.add_parser("review")
        review_p.add_argument("objective")
        review_p.add_argument("--project-root", type=Path, default=Path("."))
        review_p.add_argument("--db", type=Path, default=DEFAULT_DB)
        review_p.add_argument("--mode", default="approval-gated")
        review_p.add_argument("--max-rounds", type=int, default=5)
        review_p.add_argument("--interval", type=float, default=0.25)
        review_p.add_argument("--json", action="store_true")
        review_p.add_argument("--runner-mode", default="mock")
        review_p.add_argument("--show-transcript", action="store_true")

        for name in ("status", "brief", "transcript", "approve", "cancel"):
            p = sub.add_parser(name)
            p.add_argument("run_id")
            p.add_argument("--db", type=Path, default=DEFAULT_DB)
            if name in {"status", "brief", "transcript", "approve"}:
                p.add_argument("--json", action="store_true")

        watch_p = sub.add_parser("watch")
        watch_p.add_argument("run_id")
        watch_p.add_argument("--interval", type=float, default=5.0)
        watch_p.add_argument("--db", type=Path, default=DEFAULT_DB)

        args = parser.parse_args()
        if args.command == "init":
            init_command(args.db)
        elif args.command == "start":
            start_command(args.project_root, args.db, args.host, args.port, args.dev_auth_role, args.runner_mode)
        elif args.command == "up":
            up_command(args.project_root, args.db, args.host, args.port, args.kimi_port, not args.no_kimi, args.runner_mode)
        elif args.command == "down":
            down_command(args.db, args.port)
        elif args.command == "review":
            review_command(
                args.objective,
                args.project_root,
                args.db,
                args.mode,
                args.max_rounds,
                args.interval,
                args.json,
                args.runner_mode,
                args.show_transcript,
            )
        elif args.command == "status":
            status_command(args.run_id, args.db, args.json)
        elif args.command == "brief":
            brief_command(args.run_id, args.db, args.json)
        elif args.command == "watch":
            watch_command(args.run_id, args.interval, args.db)
        elif args.command == "transcript":
            transcript_command(args.run_id, args.db, args.json)
        elif args.command == "approve":
            approve_command(args.run_id, args.db, args.json)
        elif args.command == "cancel":
            cancel_command(args.run_id, args.db)

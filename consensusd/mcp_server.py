from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from .db import ConcurrencyError, Database
from .models import ReviewStatus, RunStatus, ToolRequest, ToolResponse, ToolRole
from .orchestrator import Orchestrator, changed_files_from_diff_stat, run_fixed_git_command
from .security import ConsensusTokenVerifier, assert_tool_allowed, bearer_token, require_localhost, role_for_token
from .settings import Settings
from .state_machine import is_terminal

SESSION_CONTEXT_CHAR_LIMIT = 12_000


def runner_mode_note(runner_mode: str) -> str:
    if runner_mode == "mock":
        return "mock runner mode verifies plumbing only; it is not a real Codex/Kimi review"
    if runner_mode.endswith("-edit"):
        return "editable runner mode: Codex may apply scoped repo changes between Kimi review rounds"
    return ""


def sanitize_session_context(session_context: Optional[str]) -> str:
    if not session_context:
        return ""
    text = str(session_context).strip()
    if not text:
        return ""
    if len(text) <= SESSION_CONTEXT_CHAR_LIMIT:
        return text
    return text[:SESSION_CONTEXT_CHAR_LIMIT].rstrip() + (
        f"\n\n... truncated at {SESSION_CONTEXT_CHAR_LIMIT} characters by consensusd session context cap ..."
    )


class ConsensusService:
    def __init__(self, db: Database, settings: Settings, orchestrator: Optional[Orchestrator] = None):
        self.db = db
        self.settings = settings
        self.orchestrator = orchestrator

    def call_tool(self, tool: str, arguments: dict[str, Any], role: ToolRole) -> dict[str, Any]:
        assert_tool_allowed(role, tool)
        handler = getattr(self, f"tool_{tool}", None)
        if handler is None:
            raise KeyError(f"unknown tool: {tool}")
        return handler(**arguments)

    def tool_start_consensus_review(
        self,
        objective: str,
        project_root: Optional[str] = None,
        mode: str = "approval-gated",
        max_rounds: int = 5,
        lease_mode: str = "detached",
        lease_ttl_seconds: int = 300,
        session_context: Optional[str] = None,
    ) -> dict[str, Any]:
        if max_rounds < 1:
            raise ValueError("max_rounds must be >= 1")
        if lease_mode not in {"detached", "attached"}:
            raise ValueError("lease_mode must be 'detached' or 'attached'")
        session_context = sanitize_session_context(session_context)
        run = self.db.create_run(
            objective,
            project_root or str(self.settings.project_root),
            mode,
            max_rounds,
            runner_mode=self.settings.runner_mode,
            lease_mode=lease_mode,
            lease_ttl_seconds=lease_ttl_seconds,
        )
        if session_context:
            self.db.add_evidence(
                run.run_id,
                1,
                "user_session_context",
                "OK",
                session_context,
                command="control_surface supplied session_context",
            )
        if self.orchestrator:
            self.orchestrator.start_background()
        return {
            "run_id": run.run_id,
            "status": run.status.value,
            "runner_mode": self.settings.runner_mode,
            "lease_mode": run.lease_mode,
            "lease_expires_at": run.lease_expires_at,
            "session_context_recorded": bool(session_context),
            "watch": f"consensusd watch {run.run_id} --db {self.settings.db_path}",
            "note": runner_mode_note(self.settings.runner_mode),
        }

    def tool_get_consensus_status(self, run_id: str) -> dict[str, Any]:
        run = self.db.get_run(run_id)
        result = run.model_dump(mode="json")
        if run.runner_mode == "mock":
            result["note"] = runner_mode_note(run.runner_mode)
        return result

    def tool_get_consensus_brief(self, run_id: str) -> dict[str, Any]:
        return build_consensus_brief(self.db.transcript(run_id))

    def tool_refresh_consensus_lease(self, run_id: str, lease_ttl_seconds: int = 90) -> dict[str, Any]:
        run = self.db.refresh_run_lease(run_id, ttl_seconds=lease_ttl_seconds)
        return {
            "run_id": run.run_id,
            "status": run.status.value,
            "lease_mode": run.lease_mode,
            "lease_expires_at": run.lease_expires_at,
        }

    def tool_watch_consensus_progress(
        self,
        run_id: str,
        wait_seconds: int = 30,
        interval_seconds: int = 5,
        refresh_lease: bool = True,
        lease_ttl_seconds: int = 90,
    ) -> dict[str, Any]:
        wait_seconds = max(1, min(wait_seconds, 120))
        interval_seconds = max(1, min(interval_seconds, wait_seconds))
        lease_ttl_seconds = max(5, min(lease_ttl_seconds, 3600))
        deadline = time.monotonic() + wait_seconds
        samples: list[dict[str, Any]] = []
        last_signature: tuple[Any, ...] | None = None
        terminal = False

        while True:
            if refresh_lease:
                self.db.refresh_run_lease(run_id, ttl_seconds=lease_ttl_seconds)
            brief = self.tool_get_consensus_brief(run_id)
            event = brief.get("latest_phase_event") or {}
            payload = event.get("payload", {}) if isinstance(event, dict) else {}
            signature = (
                brief["status"],
                brief["current_phase"],
                brief["round"],
                event.get("event_type") if isinstance(event, dict) else None,
                payload.get("heartbeat") if isinstance(payload, dict) else None,
                brief.get("artifact_counts", {}),
            )
            if signature != last_signature:
                samples.append(progress_sample(brief))
                last_signature = signature
            terminal = brief["status"] in {
                RunStatus.AWAITING_HUMAN_APPROVAL.value,
                RunStatus.RALPH_HANDOFF_COMPLETE.value,
                RunStatus.FAILED.value,
                RunStatus.CANCELLED.value,
            }
            if terminal or time.monotonic() >= deadline:
                return {
                    "run_id": run_id,
                    "terminal": terminal,
                    "elapsed_wait_seconds": wait_seconds,
                    "lease_refreshed": refresh_lease,
                    "latest": progress_sample(brief),
                    "samples": samples,
                }
            time.sleep(min(interval_seconds, max(0.0, deadline - time.monotonic())))

    def tool_get_consensus_transcript(self, run_id: str) -> dict[str, Any]:
        result = self.db.transcript(run_id).model_dump(mode="json")
        if result["run"].get("runner_mode") == "mock":
            result["run"]["note"] = runner_mode_note(result["run"]["runner_mode"])
        return result

    def tool_cancel_consensus_review(self, run_id: str) -> dict[str, Any]:
        run = self.db.get_run(run_id)
        if is_terminal(run.status):
            return run.model_dump(mode="json")
        self.db.add_event(
            run_id,
            "run.cancel_requested",
            {"status": run.status.value, "version": run.version},
        )
        updated = self.db.transition_run(run_id, RunStatus.CANCELLED, expected_version=run.version)
        return updated.model_dump(mode="json")

    def tool_approve_ralph_handoff(self, run_id: str) -> dict[str, Any]:
        run = self.db.get_run(run_id)
        if run.status != RunStatus.AWAITING_HUMAN_APPROVAL:
            raise ValueError(f"run is not awaiting approval: {run.status}")
        updated = self.db.transition_run(run_id, RunStatus.RALPH_HANDOFF_APPROVED, expected_version=run.version)
        if self.orchestrator:
            self.orchestrator.start_background()
        return updated.model_dump(mode="json")

    def tool_get_current_state(self, run_id: str) -> dict[str, Any]:
        return self.tool_get_consensus_status(run_id)

    def tool_get_proposal(self, run_id: str, round: int) -> dict[str, Any]:
        return self.db.get_proposal(run_id, round).model_dump(mode="json")

    def tool_submit_proposal(self, run_id: str, round: int, content: str, expected_version: int) -> dict[str, Any]:
        run = self.db.get_run(run_id)
        if run.status != RunStatus.CODEX_DRAFTING:
            raise ValueError(f"cannot submit proposal from {run.status}")
        if run.version != expected_version:
            raise ConcurrencyError(f"expected version {expected_version}, found {run.version}")
        proposal = self.db.add_proposal(run_id, round, content, runner="mcp_codex_planner")
        latest = self.db.get_run(run_id)
        self.db.transition_run(run_id, RunStatus.AWAITING_KIMI_REVIEW, expected_version=latest.version)
        return proposal.model_dump(mode="json")

    def tool_submit_review(self, run_id: str, round: int, status: str, content: str, expected_version: int) -> dict[str, Any]:
        run = self.db.get_run(run_id)
        if run.status != RunStatus.KIMI_REVIEWING:
            raise ValueError(f"cannot submit review from {run.status}")
        if run.version != expected_version:
            raise ConcurrencyError(f"expected version {expected_version}, found {run.version}")
        review_status = ReviewStatus(status)
        review = self.db.add_review(run_id, round, review_status, content, runner="mcp_kimi_reviewer")
        latest = self.db.get_run(run_id)
        if review_status == ReviewStatus.APPROVED:
            self.db.transition_run(run_id, RunStatus.CONSENSUS_LOCKED, expected_version=latest.version)
        else:
            self.db.transition_run(run_id, RunStatus.REVISION_REQUESTED, expected_version=latest.version, increment_round=True)
        return review.model_dump(mode="json")

    def tool_finalize_omx(self, run_id: str, content: str, expected_version: int) -> dict[str, Any]:
        run = self.db.get_run(run_id)
        if run.status != RunStatus.OMX_GENERATING:
            raise ValueError(f"cannot finalize OMX from {run.status}")
        if run.version != expected_version:
            raise ConcurrencyError(f"expected version {expected_version}, found {run.version}")
        plan = self.db.add_omx_plan(run_id, content, runner="mcp_codex_planner")
        latest = self.db.get_run(run_id)
        self.db.transition_run(run_id, RunStatus.OMX_GENERATED, expected_version=latest.version)
        return plan.model_dump(mode="json")

    def tool_get_git_diff(self, run_id: str) -> dict[str, Any]:
        run = self.db.get_run(run_id)
        status, output = run_fixed_git_command(run.project_root, "git diff")
        return {"status": status, "command": "git diff", "output": output}

    def tool_list_changed_files(self, run_id: str) -> dict[str, Any]:
        run = self.db.get_run(run_id)
        status, output = run_fixed_git_command(run.project_root, "git diff --stat")
        return {"status": status, "files": changed_files_from_diff_stat(output), "raw": output}

    def tool_record_evidence(
        self,
        run_id: str,
        round: int,
        kind: str,
        status: str,
        output: str,
        command: Optional[str] = None,
    ) -> dict[str, Any]:
        if command and command not in {"git diff", "git diff --stat"}:
            configured = " ".join(self.settings.project_test_command or ())
            if command != configured:
                raise ValueError("evidence command must be a fixed git command or configured project test command")
        evidence = self.db.add_evidence(run_id, round, kind, status, output, command)
        return evidence.model_dump(mode="json")


def _role_from_context(ctx: Context) -> ToolRole:
    token = get_access_token()
    client_id = token.client_id if token else ctx.client_id
    if not client_id:
        raise PermissionError("missing authenticated MCP caller role")
    return ToolRole(client_id)


def create_app(settings: Optional[Settings] = None, start_worker: bool = True):
    settings = settings or Settings.from_env()
    require_localhost(settings.host)
    fixed_dev_role = ToolRole(settings.dev_auth_role) if settings.dev_auth_role else None
    db = Database(settings.db_path)
    db.init()
    orchestrator = Orchestrator(db, settings)
    if should_start_worker(start_worker, fixed_dev_role):
        orchestrator.start_background()
    service = ConsensusService(db, settings, orchestrator)
    mcp = FastMCP(
        "consensusd",
        host=settings.host,
        port=settings.port,
        streamable_http_path="/mcp",
        stateless_http=True,
        token_verifier=None if fixed_dev_role else ConsensusTokenVerifier(settings),
        auth=None
        if fixed_dev_role
        else AuthSettings(
            issuer_url=f"http://{settings.host}:{settings.port}",
            resource_server_url=f"http://{settings.host}:{settings.port}/mcp",
            required_scopes=[],
        ),
    )

    def call(tool: str, ctx: Context, **arguments: Any) -> dict[str, Any]:
        role = fixed_dev_role or _role_from_context(ctx)
        return service.call_tool(tool, arguments, role)

    @mcp.tool()
    def start_consensus_review(
        objective: str,
        ctx: Context,
        project_root: Optional[str] = None,
        mode: str = "approval-gated",
        max_rounds: int = 5,
        lease_mode: str = "detached",
        lease_ttl_seconds: int = 300,
        session_context: Optional[str] = None,
    ) -> dict[str, Any]:
        return call(
            "start_consensus_review",
            ctx,
            objective=objective,
            project_root=project_root,
            mode=mode,
            max_rounds=max_rounds,
            lease_mode=lease_mode,
            lease_ttl_seconds=lease_ttl_seconds,
            session_context=session_context,
        )

    @mcp.tool()
    def get_consensus_status(run_id: str, ctx: Context) -> dict[str, Any]:
        return call("get_consensus_status", ctx, run_id=run_id)

    @mcp.tool()
    def get_consensus_brief(run_id: str, ctx: Context) -> dict[str, Any]:
        return call("get_consensus_brief", ctx, run_id=run_id)

    @mcp.tool()
    def watch_consensus_progress(
        run_id: str,
        ctx: Context,
        wait_seconds: int = 30,
        interval_seconds: int = 5,
        refresh_lease: bool = True,
        lease_ttl_seconds: int = 90,
    ) -> dict[str, Any]:
        return call(
            "watch_consensus_progress",
            ctx,
            run_id=run_id,
            wait_seconds=wait_seconds,
            interval_seconds=interval_seconds,
            refresh_lease=refresh_lease,
            lease_ttl_seconds=lease_ttl_seconds,
        )

    @mcp.tool()
    def refresh_consensus_lease(run_id: str, ctx: Context, lease_ttl_seconds: int = 90) -> dict[str, Any]:
        return call("refresh_consensus_lease", ctx, run_id=run_id, lease_ttl_seconds=lease_ttl_seconds)

    @mcp.tool()
    def get_consensus_transcript(run_id: str, ctx: Context) -> dict[str, Any]:
        return call("get_consensus_transcript", ctx, run_id=run_id)

    @mcp.tool()
    def cancel_consensus_review(run_id: str, ctx: Context) -> dict[str, Any]:
        return call("cancel_consensus_review", ctx, run_id=run_id)

    @mcp.tool()
    def approve_ralph_handoff(run_id: str, ctx: Context) -> dict[str, Any]:
        return call("approve_ralph_handoff", ctx, run_id=run_id)

    @mcp.tool()
    def get_current_state(run_id: str, ctx: Context) -> dict[str, Any]:
        return call("get_current_state", ctx, run_id=run_id)

    @mcp.tool()
    def get_proposal(run_id: str, round: int, ctx: Context) -> dict[str, Any]:
        return call("get_proposal", ctx, run_id=run_id, round=round)

    @mcp.tool()
    def submit_proposal(run_id: str, round: int, content: str, expected_version: int, ctx: Context) -> dict[str, Any]:
        return call(
            "submit_proposal",
            ctx,
            run_id=run_id,
            round=round,
            content=content,
            expected_version=expected_version,
        )

    @mcp.tool()
    def submit_review(run_id: str, round: int, status: str, content: str, expected_version: int, ctx: Context) -> dict[str, Any]:
        return call(
            "submit_review",
            ctx,
            run_id=run_id,
            round=round,
            status=status,
            content=content,
            expected_version=expected_version,
        )

    @mcp.tool()
    def finalize_omx(run_id: str, content: str, expected_version: int, ctx: Context) -> dict[str, Any]:
        return call("finalize_omx", ctx, run_id=run_id, content=content, expected_version=expected_version)

    @mcp.tool()
    def get_git_diff(run_id: str, ctx: Context) -> dict[str, Any]:
        return call("get_git_diff", ctx, run_id=run_id)

    @mcp.tool()
    def list_changed_files(run_id: str, ctx: Context) -> dict[str, Any]:
        return call("list_changed_files", ctx, run_id=run_id)

    @mcp.tool()
    def record_evidence(
        run_id: str,
        round: int,
        kind: str,
        status: str,
        output: str,
        ctx: Context,
        command: Optional[str] = None,
    ) -> dict[str, Any]:
        return call(
            "record_evidence",
            ctx,
            run_id=run_id,
            round=round,
            kind=kind,
            status=status,
            output=output,
            command=command,
        )

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/admin/runs/{run_id}", methods=["GET"])
    async def admin_run(request: Request) -> JSONResponse:
        return JSONResponse(service.db.get_run(request.path_params["run_id"]).model_dump(mode="json"))

    @mcp.custom_route("/admin/runs/{run_id}/events", methods=["GET"])
    async def admin_events(request: Request) -> JSONResponse:
        transcript = service.db.transcript(request.path_params["run_id"])
        return JSONResponse({"events": [event.model_dump(mode="json") for event in transcript.events]})

    @mcp.custom_route("/admin/runs/{run_id}/transcript", methods=["GET"])
    async def admin_transcript(request: Request) -> JSONResponse:
        return JSONResponse(service.db.transcript(request.path_params["run_id"]).model_dump(mode="json"))

    app = mcp.streamable_http_app()
    app.state.db = db
    app.state.service = service
    app.state.orchestrator = orchestrator
    app.state.settings = settings
    return app


def should_start_worker(start_worker: bool, fixed_dev_role: Optional[ToolRole]) -> bool:
    if not start_worker:
        return False
    return fixed_dev_role not in {ToolRole.CODEX_PLANNER, ToolRole.KIMI_REVIEWER}


def build_consensus_brief(transcript) -> dict[str, Any]:
    run = transcript.run
    latest_review = transcript.reviews[-1] if transcript.reviews else None
    latest_proposal = transcript.proposals[-1] if transcript.proposals else None
    latest_plan = transcript.omx_plans[-1] if transcript.omx_plans else None
    latest_bridge = transcript.context_bridges[-1] if transcript.context_bridges else None
    ralph_prompt = build_ralph_handoff_prompt(
        run,
        latest_plan.path if latest_plan else None,
        latest_bridge.path if latest_bridge else None,
    )
    latest_change = latest_evidence(transcript.evidence, "editable_change_summary")
    change_summary = parse_json(latest_change.output) if latest_change else {}
    phase_events = [
        {
            "sequence": event.sequence,
            "event_type": event.event_type,
            "payload": parse_json(event.payload_json),
            "created_at": event.created_at,
        }
        for event in transcript.events
        if event.event_type.startswith(("codex.", "kimi."))
    ]
    latest_phase_payload = phase_events[-1]["payload"] if phase_events else {}
    latest_phase_payload = latest_phase_payload if isinstance(latest_phase_payload, dict) else {}
    return {
        "run_id": run.run_id,
        "status": run.status.value,
        "error": run.error,
        "runner_mode": run.runner_mode,
        "lease_mode": run.lease_mode,
        "lease_expires_at": run.lease_expires_at,
        "round": run.current_round,
        "max_rounds": run.max_rounds,
        "objective": run.objective,
        "project_root": run.project_root,
        "current_phase": current_phase(run.status.value, phase_events),
        "latest_phase_event": phase_events[-1] if phase_events else None,
        "live_log_path": latest_phase_payload.get("live_log_path"),
        "live_log_bytes": latest_phase_payload.get("live_log_bytes"),
        "last_message_path": latest_phase_payload.get("last_message_path"),
        "last_message_exists": latest_phase_payload.get("last_message_exists"),
        "last_message_bytes": latest_phase_payload.get("last_message_bytes"),
        "last_kimi_status": latest_review.status.value if latest_review else None,
        "last_kimi_summary": summarize_text(latest_review.content, 600) if latest_review else None,
        "last_codex_summary": summarize_text(latest_proposal.content, 600) if latest_proposal else None,
        "revision_changed_files": change_summary.get("revision_changed_files", []),
        "changed_files": change_summary.get("changed_files", []),
        "guardrail_violations": change_summary.get("violations", []),
        "omx_plan_path": latest_plan.path if latest_plan else None,
        "context_bridge_path": latest_bridge.path if latest_bridge else None,
        "ralph_handoff_prompt": ralph_prompt,
        "ralph_handoff_note": ralph_handoff_note(run.status.value, bool(latest_plan)),
        "next_action": next_action(run.status.value, latest_review.status.value if latest_review else None),
        "phase_events": phase_events[-10:],
        "artifact_counts": {
            "proposals": len(transcript.proposals),
            "reviews": len(transcript.reviews),
            "evidence": len(transcript.evidence),
            "omx_plans": len(transcript.omx_plans),
            "context_bridges": len(transcript.context_bridges),
        },
    }


def build_ralph_handoff_prompt(run, plan_path: Optional[str], bridge_path: Optional[str]) -> Optional[str]:
    if run.status != RunStatus.AWAITING_HUMAN_APPROVAL:
        return None
    if not plan_path:
        return None
    plan_ref = project_relative_path(run.project_root, plan_path)
    bridge_ref = project_relative_path(run.project_root, bridge_path) if bridge_path else None
    lines = [
        f"$ralph Execute the approved consensus OMX plan at {plan_ref}.",
        "",
        "Use the consensus transcript as the source of agreement. Codex owns the implementation plan; Kimi's critique is review context that has already been adjudicated.",
    ]
    if bridge_ref:
        lines.append(f"Use the context bridge at {bridge_ref} for the Codex/Kimi disagreement summary, risks, and verification expectations.")
    lines.extend(
        [
            "",
            "Do not treat consensusd audit approval as implementation. Implement only the approved plan, preserve its safety constraints, gather fresh verification evidence, and stop only after machine-readable completion evidence is recorded.",
        ]
    )
    return "\n".join(lines)


def ralph_handoff_note(status: str, has_plan: bool) -> Optional[str]:
    if status != RunStatus.AWAITING_HUMAN_APPROVAL.value:
        return None
    if not has_plan:
        return "Ralph handoff is paused, but no OMX plan path is recorded yet."
    return (
        "Paste ralph_handoff_prompt into Codex CLI to start Ralph. "
        "approve_ralph_handoff records the consensusd audit gate; it does not itself run Ralph."
    )


def project_relative_path(project_root: str, path: Optional[str]) -> str:
    if not path:
        return ""
    try:
        return Path(path).resolve().relative_to(Path(project_root).resolve()).as_posix()
    except (OSError, ValueError):
        return str(path)


def latest_evidence(evidence, kind: str):
    matches = [item for item in evidence if item.kind == kind]
    return matches[-1] if matches else None


def progress_sample(brief: dict[str, Any]) -> dict[str, Any]:
    event = brief.get("latest_phase_event") or {}
    payload = event.get("payload", {}) if isinstance(event, dict) else {}
    heartbeat = payload.get("heartbeat") if isinstance(payload, dict) else None
    elapsed = payload.get("elapsed_seconds") if isinstance(payload, dict) else None
    return {
        "status": brief["status"],
        "phase": brief["current_phase"],
        "round": brief["round"],
        "max_rounds": brief["max_rounds"],
        "lease_mode": brief.get("lease_mode"),
        "lease_expires_at": brief.get("lease_expires_at"),
        "heartbeat": heartbeat,
        "elapsed_seconds": elapsed,
        "latest_event": event.get("event_type") if isinstance(event, dict) else None,
        "live_log_path": payload.get("live_log_path") if isinstance(payload, dict) else None,
        "live_log_bytes": payload.get("live_log_bytes") if isinstance(payload, dict) else None,
        "last_message_path": payload.get("last_message_path") if isinstance(payload, dict) else None,
        "last_message_exists": payload.get("last_message_exists") if isinstance(payload, dict) else None,
        "last_message_bytes": payload.get("last_message_bytes") if isinstance(payload, dict) else None,
        "changed_files": payload.get("changed_files", []) if isinstance(payload, dict) else [],
        "next_action": brief["next_action"],
        "last_kimi_status": brief.get("last_kimi_status"),
        "last_kimi_summary": brief.get("last_kimi_summary"),
        "last_codex_summary": brief.get("last_codex_summary"),
        "omx_plan_path": brief.get("omx_plan_path"),
        "context_bridge_path": brief.get("context_bridge_path"),
        "ralph_handoff_prompt": brief.get("ralph_handoff_prompt"),
        "ralph_handoff_note": brief.get("ralph_handoff_note"),
        "artifact_counts": brief.get("artifact_counts", {}),
        "error": brief.get("error"),
    }


def parse_json(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def summarize_text(text: str, limit: int) -> str:
    compact = " ".join(text.strip().split())
    return compact[:limit].rstrip() + ("..." if len(compact) > limit else "")


def current_phase(status: str, phase_events: list[dict[str, Any]]) -> str:
    if phase_events:
        event_type = phase_events[-1]["event_type"]
        if event_type.endswith(".heartbeat"):
            return event_type.removesuffix(".heartbeat")
        if event_type.endswith(".started"):
            return event_type.removesuffix(".started")
    return status


def next_action(status: str, last_kimi_status: Optional[str]) -> str:
    if status == RunStatus.AWAITING_HUMAN_APPROVAL.value:
        return "Review the OMX plan and approve Ralph handoff when ready."
    if status == RunStatus.FAILED.value:
        return "Inspect the latest Kimi review, editable change summary, and error before restarting."
    if status == RunStatus.REVISION_REQUESTED.value:
        return "Codex should revise the repo or proposal, then request Kimi re-review."
    if last_kimi_status == ReviewStatus.NEEDS_REVISION.value and status in {
        RunStatus.AWAITING_CODEX_PROPOSAL.value,
        RunStatus.CODEX_DRAFTING.value,
    }:
        return "Codex is preparing a revised proposal for Kimi."
    if status in {RunStatus.AWAITING_KIMI_REVIEW.value, RunStatus.KIMI_REVIEWING.value}:
        return "Wait for Kimi's architect review."
    if status in {RunStatus.CONSENSUS_LOCKED.value, RunStatus.AWAITING_OMX.value, RunStatus.OMX_GENERATING.value}:
        return "Wait for Codex to generate the OMX plan."
    if status == RunStatus.OMX_GENERATED.value:
        return "Wait for the approval gate to open."
    if status == RunStatus.RALPH_HANDOFF_COMPLETE.value:
        return "No action required; Ralph handoff is complete."
    return "Wait for the orchestrator to advance the current phase."


def create_legacy_app(settings: Optional[Settings] = None, start_worker: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    require_localhost(settings.host)
    db = Database(settings.db_path)
    db.init()
    orchestrator = Orchestrator(db, settings)
    if start_worker:
        orchestrator.start_background()
    service = ConsensusService(db, settings, orchestrator)
    app = FastAPI(title="consensusd-legacy")

    def role_dependency(token: Optional[str] = Depends(bearer_token)) -> ToolRole:
        role = role_for_token(token, settings)
        if role is None:
            raise HTTPException(status_code=401, detail="unknown caller token")
        return role

    @app.post("/legacy-mcp", response_model=ToolResponse)
    def legacy_mcp_tool(request: ToolRequest, role: ToolRole = Depends(role_dependency)) -> ToolResponse:
        try:
            return ToolResponse(ok=True, result=service.call_tool(request.tool, request.arguments, role))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except Exception as exc:
            return ToolResponse(ok=False, error=str(exc))

    return app


def app_factory():
    return create_app()

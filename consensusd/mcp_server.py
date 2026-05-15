from __future__ import annotations

import json
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


def runner_mode_note(runner_mode: str) -> str:
    if runner_mode == "mock":
        return "mock runner mode verifies plumbing only; it is not a real Codex/Kimi review"
    if runner_mode.endswith("-edit"):
        return "editable runner mode: Codex may apply scoped repo changes between Kimi review rounds"
    return ""


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
    ) -> dict[str, Any]:
        if max_rounds < 1:
            raise ValueError("max_rounds must be >= 1")
        run = self.db.create_run(
            objective,
            project_root or str(self.settings.project_root),
            mode,
            max_rounds,
            runner_mode=self.settings.runner_mode,
        )
        if self.orchestrator:
            self.orchestrator.start_background()
        return {
            "run_id": run.run_id,
            "status": run.status.value,
            "runner_mode": self.settings.runner_mode,
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

    def tool_get_consensus_transcript(self, run_id: str) -> dict[str, Any]:
        result = self.db.transcript(run_id).model_dump(mode="json")
        if result["run"].get("runner_mode") == "mock":
            result["run"]["note"] = runner_mode_note(result["run"]["runner_mode"])
        return result

    def tool_cancel_consensus_review(self, run_id: str) -> dict[str, Any]:
        run = self.db.get_run(run_id)
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
    if start_worker:
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
    ) -> dict[str, Any]:
        return call(
            "start_consensus_review",
            ctx,
            objective=objective,
            project_root=project_root,
            mode=mode,
            max_rounds=max_rounds,
        )

    @mcp.tool()
    def get_consensus_status(run_id: str, ctx: Context) -> dict[str, Any]:
        return call("get_consensus_status", ctx, run_id=run_id)

    @mcp.tool()
    def get_consensus_brief(run_id: str, ctx: Context) -> dict[str, Any]:
        return call("get_consensus_brief", ctx, run_id=run_id)

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


def build_consensus_brief(transcript) -> dict[str, Any]:
    run = transcript.run
    latest_review = transcript.reviews[-1] if transcript.reviews else None
    latest_proposal = transcript.proposals[-1] if transcript.proposals else None
    latest_plan = transcript.omx_plans[-1] if transcript.omx_plans else None
    latest_bridge = transcript.context_bridges[-1] if transcript.context_bridges else None
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
    return {
        "run_id": run.run_id,
        "status": run.status.value,
        "error": run.error,
        "runner_mode": run.runner_mode,
        "round": run.current_round,
        "max_rounds": run.max_rounds,
        "objective": run.objective,
        "project_root": run.project_root,
        "current_phase": current_phase(run.status.value, phase_events),
        "latest_phase_event": phase_events[-1] if phase_events else None,
        "last_kimi_status": latest_review.status.value if latest_review else None,
        "last_kimi_summary": summarize_text(latest_review.content, 600) if latest_review else None,
        "last_codex_summary": summarize_text(latest_proposal.content, 600) if latest_proposal else None,
        "revision_changed_files": change_summary.get("revision_changed_files", []),
        "changed_files": change_summary.get("changed_files", []),
        "guardrail_violations": change_summary.get("violations", []),
        "omx_plan_path": latest_plan.path if latest_plan else None,
        "context_bridge_path": latest_bridge.path if latest_bridge else None,
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


def latest_evidence(evidence, kind: str):
    matches = [item for item in evidence if item.kind == kind]
    return matches[-1] if matches else None


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
    if phase_events and phase_events[-1]["event_type"].endswith(".started"):
        return phase_events[-1]["event_type"]
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

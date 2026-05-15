import contextlib
import socket
import threading
import time

import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from starlette.testclient import TestClient

from consensusd.mcp_server import create_app, next_action
from consensusd.models import ReviewStatus, RunStatus, ToolRole
from consensusd.settings import Settings


def make_app(tmp_path, port=8787):
    settings = Settings.from_env(db_path=tmp_path / "consensus.sqlite", project_root=tmp_path, port=port)
    app = create_app(settings, start_worker=False)
    return settings, app


def free_port():
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class UvicornThread:
    def __init__(self, app, port):
        self.config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 5
        while not self.server.started and time.time() < deadline:
            time.sleep(0.01)
        if not self.server.started:
            raise RuntimeError("uvicorn test server did not start")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.server.should_exit = True
        self.thread.join(timeout=5)


async def call_mcp(url, token, tool, arguments):
    headers = {"Authorization": f"Bearer {token}"} if token else None
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(tool, arguments)


@pytest.mark.anyio
async def test_start_review_returns_immediately_with_run_id(tmp_path):
    port = free_port()
    settings, app = make_app(tmp_path, port=port)

    with UvicornThread(app, port):
        result = await call_mcp(
            f"http://127.0.0.1:{port}/mcp",
            settings.control_token,
            "start_consensus_review",
            {"objective": "review diff", "project_root": str(tmp_path)},
        )

    body = result.structuredContent
    assert body["run_id"].startswith("run_")
    assert body["status"] == RunStatus.INIT.value


@pytest.mark.anyio
async def test_role_permission_rejects_wrong_tool(tmp_path):
    port = free_port()
    settings, app = make_app(tmp_path, port=port)

    with UvicornThread(app, port):
        result = await call_mcp(
            f"http://127.0.0.1:{port}/mcp",
            settings.kimi_token,
            "start_consensus_review",
            {"objective": "review diff", "project_root": str(tmp_path)},
        )

    assert result.isError is True
    assert "cannot call start_consensus_review" in result.content[0].text


@pytest.mark.anyio
async def test_dev_auth_role_allows_tokenless_control_surface(tmp_path):
    port = free_port()
    settings = Settings.from_env(
        db_path=tmp_path / "consensus.sqlite",
        project_root=tmp_path,
        port=port,
        dev_auth_role="control_surface",
    )
    app = create_app(settings, start_worker=False)

    with UvicornThread(app, port):
        result = await call_mcp(
            f"http://127.0.0.1:{port}/mcp",
            None,
            "start_consensus_review",
            {"objective": "review diff", "project_root": str(tmp_path)},
        )

    assert result.structuredContent["run_id"].startswith("run_")


@pytest.mark.anyio
async def test_dev_auth_role_still_enforces_role_permissions(tmp_path):
    port = free_port()
    settings = Settings.from_env(
        db_path=tmp_path / "consensus.sqlite",
        project_root=tmp_path,
        port=port,
        dev_auth_role="kimi_reviewer",
    )
    app = create_app(settings, start_worker=False)

    with UvicornThread(app, port):
        result = await call_mcp(
            f"http://127.0.0.1:{port}/mcp",
            None,
            "start_consensus_review",
            {"objective": "review diff", "project_root": str(tmp_path)},
        )

    assert result.isError is True
    assert "cannot call start_consensus_review" in result.content[0].text


@pytest.mark.anyio
async def test_mcp_lists_consensus_tools(tmp_path):
    port = free_port()
    settings, app = make_app(tmp_path, port=port)

    with UvicornThread(app, port):
        async with streamablehttp_client(
            f"http://127.0.0.1:{port}/mcp",
            headers={"Authorization": f"Bearer {settings.control_token}"},
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()

    names = {tool.name for tool in tools.tools}
    assert "start_consensus_review" in names
    assert "get_consensus_brief" in names
    assert "watch_consensus_progress" in names
    assert "refresh_consensus_lease" in names
    assert "approve_ralph_handoff" in names


def test_healthz_admin_route(tmp_path):
    settings, app = make_app(tmp_path)
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_agent_role_endpoint_does_not_start_orchestrator_worker(tmp_path):
    settings = Settings.from_env(
        db_path=tmp_path / "consensus.sqlite",
        project_root=tmp_path,
        dev_auth_role=ToolRole.KIMI_REVIEWER.value,
    )

    app = create_app(settings, start_worker=True)

    assert app.state.orchestrator._thread is None


def test_legacy_json_shim_available_for_low_level_tests(tmp_path):
    from consensusd.mcp_server import create_legacy_app

    settings = Settings.from_env(db_path=tmp_path / "legacy.sqlite", project_root=tmp_path)
    client = TestClient(create_legacy_app(settings, start_worker=False))

    response = client.post(
        "/legacy-mcp",
        headers={"Authorization": f"Bearer {settings.control_token}"},
        json={
            "tool": "start_consensus_review",
            "arguments": {"objective": "review diff", "project_root": str(tmp_path)},
        },
    )

    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is True
    assert body["result"]["run_id"].startswith("run_")


def test_legacy_role_permission_rejects_wrong_tool(tmp_path):
    from consensusd.mcp_server import create_legacy_app

    settings = Settings.from_env(db_path=tmp_path / "legacy.sqlite", project_root=tmp_path)
    client = TestClient(create_legacy_app(settings, start_worker=False))

    response = client.post(
        "/legacy-mcp",
        headers={"Authorization": f"Bearer {settings.kimi_token}"},
        json={
            "tool": "start_consensus_review",
            "arguments": {"objective": "review diff", "project_root": str(tmp_path)},
        },
    )

    assert response.status_code == 403


def test_review_rejection_and_approval_paths_via_service(tmp_path):
    settings, app = make_app(tmp_path)
    service = app.state.service
    service.orchestrator = None
    run = service.db.create_run("review diff", str(tmp_path), "approval-gated")
    r1 = service.db.transition_run(run.run_id, RunStatus.AWAITING_CODEX_PROPOSAL, expected_version=0)
    r1 = service.db.transition_run(run.run_id, RunStatus.CODEX_DRAFTING, expected_version=r1.version)

    proposal = service.tool_submit_proposal(run.run_id, 1, "proposal", r1.version)
    run_after_proposal = service.db.get_run(run.run_id)
    run_after_proposal = service.db.transition_run(
        run.run_id,
        RunStatus.KIMI_REVIEWING,
        expected_version=run_after_proposal.version,
    )
    review = service.tool_submit_review(
        run.run_id,
        1,
        "NEEDS_REVISION",
        "tighten security",
        run_after_proposal.version,
    )

    assert proposal["content"] == "proposal"
    assert review["status"] == "NEEDS_REVISION"
    assert service.db.get_run(run.run_id).status == RunStatus.REVISION_REQUESTED


def test_verification_tools_use_fixed_git_commands(tmp_path):
    settings, app = make_app(tmp_path)
    service = app.state.service
    run = service.db.create_run("review diff", str(tmp_path), "approval-gated")

    diff = service.tool_get_git_diff(run.run_id)
    files = service.tool_list_changed_files(run.run_id)

    assert diff["command"] == "git diff"
    assert "files" in files


def test_verification_tools_return_concise_non_git_error(tmp_path):
    settings, app = make_app(tmp_path)
    service = app.state.service
    run = service.db.create_run("review diff", str(tmp_path), "approval-gated")

    diff = service.tool_get_git_diff(run.run_id)

    assert diff["status"] == "FAILED"
    assert diff["output"] == f"not a git repository: {tmp_path}"


def test_record_evidence_rejects_arbitrary_command(tmp_path):
    settings, app = make_app(tmp_path)
    service = app.state.service
    run = service.db.create_run("review diff", str(tmp_path), "approval-gated")

    try:
        service.tool_record_evidence(run.run_id, 1, "test", "OK", "nope", command="python arbitrary.py")
    except ValueError as exc:
        assert "fixed git command" in str(exc)
    else:
        raise AssertionError("arbitrary evidence command accepted")


def test_consensus_brief_summarizes_run_for_codex(tmp_path):
    settings, app = make_app(tmp_path)
    service = app.state.service
    run = service.db.create_run("review diff", str(tmp_path), "approval-gated")
    service.db.add_event(run.run_id, "codex.proposal.started", {"round": 1})
    service.db.add_proposal(run.run_id, 1, "Codex proposal content")
    service.db.add_review(run.run_id, 1, ReviewStatus.NEEDS_REVISION, "Kimi wants stronger evidence")
    service.db.add_omx_plan(run.run_id, "plan", path="/tmp/plan.md")
    service.db.add_context_bridge(run.run_id, "/tmp/bridge.md", "bridge")

    brief = service.tool_get_consensus_brief(run.run_id)

    assert brief["run_id"] == run.run_id
    assert brief["error"] is None
    assert brief["current_phase"] == "codex.proposal"
    assert brief["latest_phase_event"]["event_type"] == "codex.proposal.started"
    assert brief["last_kimi_status"] == "NEEDS_REVISION"
    assert "Kimi wants" in brief["last_kimi_summary"]
    assert brief["omx_plan_path"] == "/tmp/plan.md"
    assert brief["context_bridge_path"] == "/tmp/bridge.md"
    assert brief["artifact_counts"]["context_bridges"] == 1


def test_watch_consensus_progress_returns_codex_friendly_samples(tmp_path):
    settings, app = make_app(tmp_path)
    service = app.state.service
    run = service.db.create_run("review diff", str(tmp_path), "approval-gated")
    service.db.add_event(
        run.run_id,
        "codex.proposal.heartbeat",
        {"round": 1, "elapsed_seconds": 31.2, "heartbeat": 1},
    )

    progress = service.tool_watch_consensus_progress(run.run_id, wait_seconds=1, interval_seconds=1)

    assert progress["run_id"] == run.run_id
    assert progress["terminal"] is False
    assert progress["samples"]
    assert progress["latest"]["phase"] == "codex.proposal"
    assert progress["latest"]["heartbeat"] == 1
    assert progress["latest"]["next_action"]


def test_watch_consensus_progress_surfaces_live_revision_changed_files(tmp_path):
    settings, app = make_app(tmp_path)
    service = app.state.service
    run = service.db.create_run("review diff", str(tmp_path), "approval-gated")
    service.db.add_event(
        run.run_id,
        "codex.revision.heartbeat",
        {
            "round": 2,
            "elapsed_seconds": 12.5,
            "heartbeat": 1,
            "changed_files": ["src/guard.mjs", "tests/guard.test.mjs"],
        },
    )

    progress = service.tool_watch_consensus_progress(run.run_id, wait_seconds=1, interval_seconds=1)

    assert progress["latest"]["phase"] == "codex.revision"
    assert progress["latest"]["changed_files"] == ["src/guard.mjs", "tests/guard.test.mjs"]


def test_start_review_can_attach_lease_and_watch_refreshes_it(tmp_path):
    settings, app = make_app(tmp_path)
    service = app.state.service
    service.orchestrator = None
    result = service.tool_start_consensus_review(
        "review diff",
        str(tmp_path),
        lease_mode="attached",
        lease_ttl_seconds=5,
    )

    before = service.db.get_run(result["run_id"])
    progress = service.tool_watch_consensus_progress(
        result["run_id"],
        wait_seconds=1,
        interval_seconds=1,
        refresh_lease=True,
        lease_ttl_seconds=30,
    )
    after = service.db.get_run(result["run_id"])

    assert result["lease_mode"] == "attached"
    assert before.lease_expires_at is not None
    assert after.lease_expires_at is not None
    assert after.lease_expires_at > before.lease_expires_at
    assert progress["lease_refreshed"] is True


def test_start_review_records_control_surface_session_context(tmp_path):
    settings, app = make_app(tmp_path)
    service = app.state.service
    service.orchestrator = None
    context = (
        "Ralph just completed P11B.1 and reported guard/test artifacts. "
        "User now asks to review the P11B implementation and plan the next safest step."
    )

    result = service.tool_start_consensus_review(
        "review P11B implementation",
        str(tmp_path),
        session_context=context,
    )

    evidence = service.db.list_evidence(result["run_id"])
    assert result["session_context_recorded"] is True
    assert len(evidence) == 1
    assert evidence[0].kind == "user_session_context"
    assert evidence[0].command == "control_surface supplied session_context"
    assert context in evidence[0].output


def test_consensus_brief_includes_failed_run_error(tmp_path):
    settings, app = make_app(tmp_path)
    service = app.state.service
    run = service.db.create_run("review diff", str(tmp_path), "approval-gated")
    service.db.transition_run(run.run_id, RunStatus.FAILED, expected_version=run.version, error="guardrail failed")

    brief = service.tool_get_consensus_brief(run.run_id)

    assert brief["status"] == RunStatus.FAILED.value
    assert brief["error"] == "guardrail failed"


def test_next_action_does_not_use_stale_kimi_revision_during_review():
    assert (
        next_action(RunStatus.KIMI_REVIEWING.value, ReviewStatus.NEEDS_REVISION.value)
        == "Wait for Kimi's architect review."
    )
    assert (
        next_action(RunStatus.CODEX_DRAFTING.value, ReviewStatus.NEEDS_REVISION.value)
        == "Codex is preparing a revised proposal for Kimi."
    )

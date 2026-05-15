from pathlib import Path
import json
import subprocess
import time

from consensusd.db import Database
from consensusd.mcp_server import ConsensusService
from consensusd.models import Evidence, Proposal, Review, ReviewDecision, ReviewStatus, Run, RunStatus
from consensusd.orchestrator import Orchestrator, editable_path_violation, git_changed_files
from consensusd.settings import Settings


def make_service(tmp_path, max_rounds=5):
    settings = Settings.from_env(db_path=tmp_path / "consensus.sqlite", project_root=tmp_path)
    db = Database(settings.db_path)
    db.init()
    orchestrator = Orchestrator(db, settings)
    service = ConsensusService(db, settings, orchestrator)
    run = db.create_run(
        "ship consensusd with a deliberately long objective that should not produce painful artifact filenames",
        str(tmp_path),
        "approval-gated",
        max_rounds=max_rounds,
    )
    return service, orchestrator, run


def init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)
    (path / ".gitkeep").write_text("keep\n")
    subprocess.run(["git", "add", ".gitkeep"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )


def test_mock_orchestrator_full_run_to_approval_gate(tmp_path):
    service, orchestrator, run = make_service(tmp_path)

    orchestrator.tick_until_idle()

    final = service.db.get_run(run.run_id)
    transcript = service.db.transcript(run.run_id)
    assert final.status == RunStatus.AWAITING_HUMAN_APPROVAL
    assert final.current_round == 2
    assert [review.status.value for review in transcript.reviews] == ["NEEDS_REVISION", "APPROVED"]
    assert transcript.omx_plans
    assert transcript.context_bridges
    assert Path(transcript.omx_plans[-1].path).name.startswith("consensus-omx-")
    assert Path(transcript.context_bridges[-1].path).name.startswith("context-bridge-")
    assert len(Path(transcript.omx_plans[-1].path).name) < 80
    assert len(Path(transcript.context_bridges[-1].path).name) < 80
    bridge = transcript.context_bridges[-1]
    assert "Kimi-Codex Context Bridge" in bridge.content
    assert "Review Cycle Summary" in bridge.content
    assert "Ralph Handoff Context" in bridge.content
    assert service.db.path.exists()


def test_max_rounds_failure(tmp_path):
    service, orchestrator, run = make_service(tmp_path, max_rounds=1)

    for _ in range(20):
        orchestrator.tick()
        if service.db.get_run(run.run_id).status == RunStatus.FAILED:
            break

    failed = service.db.get_run(run.run_id)
    assert failed.status == RunStatus.FAILED
    assert "max rounds exceeded" in (failed.error or "")


def test_approval_gated_ralph_handoff(tmp_path):
    service, orchestrator, run = make_service(tmp_path)
    orchestrator.tick_until_idle()

    approved = service.tool_approve_ralph_handoff(run.run_id)
    assert approved["status"] == RunStatus.RALPH_HANDOFF_APPROVED.value

    orchestrator.tick_until_idle()
    final = service.db.get_run(run.run_id)
    transcript = service.db.transcript(run.run_id)
    assert final.status == RunStatus.RALPH_HANDOFF_COMPLETE
    assert transcript.ralph_handoffs


class EditingCodexRunner:
    def generate_proposal(self, run: Run, prior_review: Review | None, evidence: list[Evidence]) -> str:
        fixed = (tmp_file := Path(run.project_root) / "fixed.txt").exists()
        prior = prior_review.content if prior_review else "initial"
        return f"# Proposal round {run.current_round}\n\nfixed={fixed}\n\nprior={prior}\n\npath={tmp_file}"

    def apply_revision(self, run: Run, prior_review: Review, evidence: list[Evidence]) -> str:
        path = Path(run.project_root) / "fixed.txt"
        path.write_text("fixed\n")
        return f"## Revision Summary\n\nCreated `{path.name}` to resolve: {prior_review.content}"

    def review_proposal(self, run: Run, proposal: Proposal, evidence: list[Evidence]) -> ReviewDecision:
        raise NotImplementedError

    def generate_omx(self, run: Run, consensus_proposal: Proposal, evidence: list[Evidence]) -> str:
        return "# OMX\n\nEditable loop reached consensus."


class FileAwareKimiRunner:
    def generate_proposal(self, run: Run, prior_review: Review | None, evidence: list[Evidence]) -> str:
        raise NotImplementedError

    def review_proposal(self, run: Run, proposal: Proposal, evidence: list[Evidence]) -> ReviewDecision:
        fixed = Path(run.project_root, "fixed.txt").exists()
        if not fixed:
            return ReviewDecision(status=ReviewStatus.NEEDS_REVISION, content="C1: create fixed.txt before approval")
        return ReviewDecision(status=ReviewStatus.APPROVED, content="Approved after real Codex revision evidence.")

    def generate_omx(self, run: Run, consensus_proposal: Proposal, evidence: list[Evidence]) -> str:
        raise NotImplementedError


def test_editable_runner_applies_revision_between_kimi_rounds(tmp_path):
    init_git_repo(tmp_path)
    settings = Settings.from_env(db_path=tmp_path / "consensus.sqlite", project_root=tmp_path)
    db = Database(settings.db_path)
    db.init()
    run = db.create_run(
        "fix until Kimi approves",
        str(tmp_path),
        "approval-gated",
        max_rounds=3,
        runner_mode="codex-kimi-edit",
    )
    orchestrator = Orchestrator(db, settings, codex_runner=EditingCodexRunner(), kimi_runner=FileAwareKimiRunner())

    orchestrator.tick_until_idle()

    final = db.get_run(run.run_id)
    transcript = db.transcript(run.run_id)
    assert final.status == RunStatus.AWAITING_HUMAN_APPROVAL
    assert final.current_round == 2
    assert (tmp_path / "fixed.txt").read_text() == "fixed\n"
    assert [review.status for review in transcript.reviews] == [ReviewStatus.NEEDS_REVISION, ReviewStatus.APPROVED]
    assert any(item.kind == "codex_revision" for item in transcript.evidence)
    assert any(item.kind == "git_diff_stat_after_codex_revision" for item in transcript.evidence)
    change_summary = [item for item in transcript.evidence if item.kind == "editable_change_summary"][-1]
    assert "fixed.txt" in change_summary.output
    audited = [event for event in transcript.events if event.event_type == "codex.revision.audited"][-1]
    assert json.loads(audited.payload_json)["violations"] == []
    assert [event.event_type for event in transcript.events if event.event_type.startswith("codex.revision")] == [
        "codex.revision.started",
        "codex.revision.completed",
        "codex.revision.audited",
    ]


class DeniedPathCodexRunner(EditingCodexRunner):
    def apply_revision(self, run: Run, prior_review: Review, evidence: list[Evidence]) -> str:
        path = Path(run.project_root) / ".consensusd" / "unsafe.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("unsafe\n")
        return "## Revision Summary\n\nTouched denied path."


def test_editable_runner_fails_when_revision_touches_denied_path(tmp_path):
    init_git_repo(tmp_path)
    settings = Settings.from_env(db_path=tmp_path / "consensus.sqlite", project_root=tmp_path)
    db = Database(settings.db_path)
    db.init()
    run = db.create_run(
        "do not touch local state",
        str(tmp_path),
        "approval-gated",
        max_rounds=3,
        runner_mode="codex-kimi-edit",
    )
    orchestrator = Orchestrator(db, settings, codex_runner=DeniedPathCodexRunner(), kimi_runner=FileAwareKimiRunner())

    for _ in range(20):
        orchestrator.tick()
        if db.get_run(run.run_id).status == RunStatus.FAILED:
            break

    final = db.get_run(run.run_id)
    transcript = db.transcript(run.run_id)
    assert final.status == RunStatus.FAILED
    assert ".consensusd/" in (final.error or "")
    change_summary = [item for item in transcript.evidence if item.kind == "editable_change_summary"][-1]
    assert change_summary.status == "FAILED"
    assert ".consensusd/" in change_summary.output


def test_editable_runner_requires_git_worktree_before_revision(tmp_path):
    settings = Settings.from_env(db_path=tmp_path / "consensus.sqlite", project_root=tmp_path)
    db = Database(settings.db_path)
    db.init()
    run = db.create_run(
        "editable needs git",
        str(tmp_path),
        "approval-gated",
        max_rounds=3,
        runner_mode="codex-kimi-edit",
    )
    orchestrator = Orchestrator(db, settings, codex_runner=EditingCodexRunner(), kimi_runner=FileAwareKimiRunner())

    for _ in range(20):
        orchestrator.tick()
        if db.get_run(run.run_id).status == RunStatus.FAILED:
            break

    final = db.get_run(run.run_id)
    assert final.status == RunStatus.FAILED
    assert "editable mode requires a git worktree" in (final.error or "")
    assert not (tmp_path / "fixed.txt").exists()


def test_editable_guardrails_use_path_boundaries():
    settings = Settings(editable_allowed_paths=("src/",), editable_denied_paths=(".env", ".env.", "secrets/"))

    assert not editable_path_violation("src/app.py", settings)
    assert editable_path_violation("src-old/app.py", settings)
    assert editable_path_violation(".env", settings)
    assert editable_path_violation(".env.local", settings)
    assert editable_path_violation("secrets/key.txt", settings)


def test_git_changed_files_handles_spaces_and_renames(tmp_path):
    init_git_repo(tmp_path)
    original = tmp_path / "name with space.txt"
    original.write_text("before\n")
    subprocess.run(["git", "add", "name with space.txt"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "space file"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    renamed = tmp_path / "renamed file.txt"
    subprocess.run(["git", "mv", "name with space.txt", "renamed file.txt"], cwd=tmp_path, check=True)
    (tmp_path / "new file.txt").write_text("new\n")

    assert git_changed_files(str(tmp_path)) == ["new file.txt", "renamed file.txt"]


class SlowApprovingKimiRunner(FileAwareKimiRunner):
    def review_proposal(self, run: Run, proposal: Proposal, evidence: list[Evidence]) -> ReviewDecision:
        time.sleep(0.03)
        return ReviewDecision(status=ReviewStatus.APPROVED, content="Approved after heartbeat.")


def test_long_runner_phase_emits_heartbeat_events(tmp_path):
    settings = Settings(db_path=tmp_path / "consensus.sqlite", project_root=tmp_path, heartbeat_interval_sec=0.01)
    db = Database(settings.db_path)
    db.init()
    run = db.create_run("heartbeat", str(tmp_path), "approval-gated", max_rounds=2)
    orchestrator = Orchestrator(db, settings, kimi_runner=SlowApprovingKimiRunner())

    orchestrator.tick_until_idle()

    transcript = db.transcript(run.run_id)
    assert any(event.event_type == "kimi.review.heartbeat" for event in transcript.events)
    assert db.get_run(run.run_id).status == RunStatus.AWAITING_HUMAN_APPROVAL

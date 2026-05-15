from pathlib import Path

from consensusd.db import Database
from consensusd.mcp_server import ConsensusService
from consensusd.models import Evidence, Proposal, Review, ReviewDecision, ReviewStatus, Run, RunStatus
from consensusd.orchestrator import Orchestrator
from consensusd.settings import Settings


def make_service(tmp_path, max_rounds=5):
    settings = Settings.from_env(db_path=tmp_path / "consensus.sqlite", project_root=tmp_path)
    db = Database(settings.db_path)
    db.init()
    orchestrator = Orchestrator(db, settings)
    service = ConsensusService(db, settings, orchestrator)
    run = db.create_run("ship consensusd", str(tmp_path), "approval-gated", max_rounds=max_rounds)
    return service, orchestrator, run


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

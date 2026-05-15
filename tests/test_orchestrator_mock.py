from pathlib import Path
import json
import subprocess
import time

from consensusd.db import Database
from consensusd.mcp_server import ConsensusService
from consensusd.models import Evidence, Proposal, Review, ReviewDecision, ReviewStatus, Run, RunStatus
from consensusd.orchestrator import (
    Orchestrator,
    build_deep_context_evidence,
    editable_path_violation,
    extract_context_file_paths,
    extract_commit_refs,
    git_changed_files,
    initial_git_evidence_commands,
)
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
    packets = [item for item in transcript.evidence if item.kind == "kimi_review_packet"]
    assert [item.round for item in packets] == [1, 2]
    assert "Kimi Review Evidence Packet" in packets[-1].output
    packet_path = packets[-1].output.splitlines()[0].removeprefix("Packet path: ")
    assert Path(packet_path).exists()
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


def test_attached_lease_expiry_does_not_cancel_human_approval_gate(tmp_path):
    settings = Settings.from_env(db_path=tmp_path / "consensus.sqlite", project_root=tmp_path)
    db = Database(settings.db_path)
    db.init()
    orchestrator = Orchestrator(db, settings)
    run = db.create_run(
        "pause safely at approval gate",
        str(tmp_path),
        "approval-gated",
        lease_mode="attached",
        lease_ttl_seconds=1,
    )
    for status in [
        RunStatus.AWAITING_CODEX_PROPOSAL,
        RunStatus.CODEX_DRAFTING,
        RunStatus.AWAITING_KIMI_REVIEW,
        RunStatus.KIMI_REVIEWING,
        RunStatus.CONSENSUS_LOCKED,
        RunStatus.AWAITING_OMX,
        RunStatus.OMX_GENERATING,
        RunStatus.OMX_GENERATED,
        RunStatus.AWAITING_HUMAN_APPROVAL,
    ]:
        run = db.transition_run(run.run_id, status, expected_version=run.version)

    time.sleep(1.1)

    assert orchestrator._cancel_if_lease_expired(run.run_id) is False
    transcript = db.transcript(run.run_id)
    assert transcript.run.status == RunStatus.AWAITING_HUMAN_APPROVAL
    assert all(event.event_type != "run.lease_expired" for event in transcript.events)


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


class CancellingCodexRunner:
    def __init__(self, db: Database):
        self.db = db

    def generate_proposal(self, run: Run, prior_review: Review | None, evidence: list[Evidence]) -> str:
        self.db.transition_run(run.run_id, RunStatus.CANCELLED, expected_version=run.version)
        return "# Late proposal after cancellation"

    def review_proposal(self, run: Run, proposal: Proposal, evidence: list[Evidence]) -> ReviewDecision:
        raise NotImplementedError

    def generate_omx(self, run: Run, consensus_proposal: Proposal, evidence: list[Evidence]) -> str:
        raise NotImplementedError


def test_cancel_during_runner_prevents_late_proposal_write(tmp_path):
    settings = Settings(db_path=tmp_path / "consensus.sqlite", project_root=tmp_path)
    db = Database(settings.db_path)
    db.init()
    run = db.create_run("cancel while codex is drafting", str(tmp_path), "approval-gated")
    orchestrator = Orchestrator(db, settings, codex_runner=CancellingCodexRunner(db))

    orchestrator.tick()
    orchestrator.tick()
    orchestrator.tick()

    transcript = db.transcript(run.run_id)
    assert transcript.run.status == RunStatus.CANCELLED
    assert transcript.proposals == []


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


def test_initial_repo_evidence_anchors_to_objective_commit_without_unrelated_head(tmp_path):
    init_git_repo(tmp_path)
    changed = tmp_path / "p11b.txt"
    changed.write_text("readonly guard\n")
    subprocess.run(["git", "add", "p11b.txt"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "p11b guard"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    commit = subprocess.run(
        ["git", "rev-parse", "--short=12", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    cleanup = tmp_path / "consensus-review.skill.md"
    cleanup.write_text("local agent tooling cleanup\n")
    subprocess.run(["git", "add", "consensus-review.skill.md"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "clean repo request"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    (tmp_path / "unrelated-dirty.txt").write_text("dirty\n")

    settings = Settings(db_path=tmp_path / "consensus.sqlite", project_root=tmp_path)
    db = Database(settings.db_path)
    db.init()
    run = db.create_run(
        f"review the next step after commit {commit}",
        str(tmp_path),
        "approval-gated",
        max_rounds=1,
    )
    orchestrator = Orchestrator(db, settings)

    orchestrator.tick()

    by_kind = {item.kind: item for item in db.list_evidence(run.run_id)}
    assert "git_status_short" in by_kind
    assert "git_diff_stat" in by_kind
    assert "git_head_summary" not in by_kind
    assert "git_head_name_status" not in by_kind
    assert "git_objective_commit_summary" in by_kind
    assert "git_objective_commit_name_status" in by_kind
    assert "context_scope_note" in by_kind
    assert "objective_commit_diff" in by_kind
    assert "deep_context_file_inventory" in by_kind
    assert "deep_context_file_contents" in by_kind
    assert "unrelated-dirty.txt" in by_kind["git_status_short"].output
    assert "clean repo request" not in "\n".join(item.output for item in by_kind.values())
    assert "consensus-review.skill.md" not in by_kind["git_objective_commit_name_status"].output
    assert "p11b.txt" in by_kind["git_objective_commit_name_status"].output
    assert "readonly guard" in by_kind["objective_commit_diff"].output
    assert "p11b.txt" in by_kind["deep_context_file_contents"].output


def test_initial_repo_evidence_includes_head_when_no_objective_commit(tmp_path):
    init_git_repo(tmp_path)
    changed = tmp_path / "general.txt"
    changed.write_text("general change\n")
    subprocess.run(["git", "add", "general.txt"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "general head"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "git_head_summary" in {kind for kind, _, _ in initial_git_evidence_commands("review current diff")}


def test_session_context_without_commit_omits_git_evidence(tmp_path):
    init_git_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "revolut-p11b-collector.mjs").write_text("P11B collector context\n")
    subprocess.run(["git", "add", "src/revolut-p11b-collector.mjs"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "p11b implementation"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    (tmp_path / "cleanup.txt").write_text("unrelated dirty cleanup\n")
    settings = Settings(db_path=tmp_path / "consensus.sqlite", project_root=tmp_path)
    db = Database(settings.db_path)
    db.init()
    run = db.create_run("review the P11B implementation and plan the next step", str(tmp_path), "approval-gated", max_rounds=1)
    db.add_evidence(
        run.run_id,
        1,
        "user_session_context",
        "OK",
        "Ralph just completed P11B.1 and edited src/revolut-p11b-collector.mjs.",
        command="control_surface supplied session_context",
    )
    orchestrator = Orchestrator(db, settings)

    orchestrator.tick()

    by_kind = {item.kind: item for item in db.list_evidence(run.run_id)}
    assert "user_session_context" in by_kind
    assert "context_scope_note" in by_kind
    assert "git_status_short" not in by_kind
    assert "git_diff_stat" not in by_kind
    assert "git_head_summary" not in by_kind
    assert "git_head_name_status" not in by_kind
    assert "unrelated dirty cleanup" not in "\n".join(item.output for item in by_kind.values())
    assert "P11B collector context" in by_kind["deep_context_file_contents"].output


def test_session_context_file_paths_drive_context_inventory(tmp_path):
    init_git_repo(tmp_path)
    (tmp_path / ".omx" / "context").mkdir(parents=True)
    (tmp_path / ".omx" / "context" / "p11b-ralph.md").write_text("Ralph P11B summary\n")
    (tmp_path / "src" / "data").mkdir(parents=True)
    (tmp_path / "src" / "data" / "revolut-x-readonly-collector-guard.mjs").write_text("P11B guard\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "phase10-revolut-x-private-readonly-balance-preflight.test.mjs").write_text("phase10 noise\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "context files"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    evidence = build_deep_context_evidence(
        str(tmp_path),
        "review the P11B implementation",
        session_context=(
            "Use .omx/context/p11b-ralph.md and "
            "src/data/revolut-x-readonly-collector-guard.mjs as the review anchor."
        ),
    )
    by_kind = {item["kind"]: item for item in evidence}

    assert by_kind["deep_context_file_inventory"]["command"] == "session_context path extraction plus P11B artifact bundle"
    assert by_kind["deep_context_file_inventory"]["output"].splitlines() == [
        ".omx/context/p11b-ralph.md",
        "src/data/revolut-x-readonly-collector-guard.mjs",
    ]
    assert "phase10 noise" not in by_kind["deep_context_file_contents"]["output"]


def test_session_context_adds_p11b_artifact_bundle_without_git_scope(tmp_path):
    init_git_repo(tmp_path)
    (tmp_path / ".omx" / "plans").mkdir(parents=True)
    (tmp_path / ".omx" / "security").mkdir(parents=True)
    (tmp_path / ".omx" / "control").mkdir(parents=True)
    (tmp_path / ".omx" / "validation" / "p11b-final").mkdir(parents=True)
    (tmp_path / ".omx" / "plans" / "final-p11b-authenticated-read-only-data-contract.md").write_text("final plan\n")
    (tmp_path / ".omx" / "security" / "revolut-x-p11b-key-governance.md").write_text("key governance\n")
    (tmp_path / ".omx" / "control" / "revolut-x-p11b-readonly-control-template.json").write_text("{}\n")
    (tmp_path / ".omx" / "validation" / "p11b-final" / "completion-audit.json").write_text('{"passed":true}\n')
    (tmp_path / "src" / "data").mkdir(parents=True)
    (tmp_path / "src" / "data" / "revolut-x-readonly-collector-guard.mjs").write_text("guard\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "revolut-x-readonly-collector-guard.test.mjs").write_text("tests\n")

    evidence = build_deep_context_evidence(
        str(tmp_path),
        "review latest P11B implementation",
        session_context="Ralph just completed P11B.1; current ask is next safest step.",
    )
    by_kind = {item["kind"]: item for item in evidence}
    inventory = by_kind["deep_context_file_inventory"]["output"]

    assert by_kind["deep_context_file_inventory"]["command"] == "P11B artifact bundle"
    assert "src/data/revolut-x-readonly-collector-guard.mjs" in inventory
    assert "tests/revolut-x-readonly-collector-guard.test.mjs" in inventory
    assert ".omx/plans/final-p11b-authenticated-read-only-data-contract.md" in inventory
    assert ".omx/security/revolut-x-p11b-key-governance.md" in inventory
    assert ".omx/control/revolut-x-p11b-readonly-control-template.json" in inventory
    assert ".omx/validation/p11b-final/completion-audit.json" in inventory


def test_extract_context_file_paths_preserves_dot_paths():
    assert extract_context_file_paths("See `.omx/context/p11b.md`; ./src/data/guard.mjs.") == [
        ".omx/context/p11b.md",
        "src/data/guard.mjs",
    ]


def test_extract_context_file_paths_preserves_json_suffix():
    assert extract_context_file_paths("Use .omx/security/revolut-x-readonly-control-template.json.") == [
        ".omx/security/revolut-x-readonly-control-template.json"
    ]


def test_deep_context_evidence_excludes_private_local_paths(tmp_path):
    init_git_repo(tmp_path)
    (tmp_path / ".env").write_text("SECRET=do-not-read\n")
    (tmp_path / ".consensusd").mkdir()
    (tmp_path / ".consensusd" / "private.txt").write_text("do-not-read\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "revolut-p11b-collector.mjs").write_text("safe context\n")
    subprocess.run(["git", "add", "src/revolut-p11b-collector.mjs"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "add p11b context"], cwd=tmp_path, check=True, capture_output=True, text=True)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True).stdout.strip()

    evidence = build_deep_context_evidence(str(tmp_path), f"review {commit}")
    joined = "\n".join(item["output"] for item in evidence)

    assert "safe context" in joined
    assert "do-not-read" not in joined


def test_extract_commit_refs_ignores_non_hex_words():
    assert extract_commit_refs("after commit 6f80e97 and ticket p11b") == ["6f80e97"]


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

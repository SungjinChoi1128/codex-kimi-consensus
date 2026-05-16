import sys

from consensusd.models import Evidence
from consensusd.runners.subprocess_codex import _proposal_context_instruction
from consensusd.runners.subprocess_kimi import _review_context_instruction
from consensusd.runners.subprocess_utils import CommandResult, extract_codex_thread_id, extract_kimi_session_id


def evidence(kind: str) -> Evidence:
    return Evidence(
        evidence_id=f"evidence_{kind}",
        run_id="run_prompt",
        round=1,
        kind=kind,
        command=None,
        status="OK",
        output="context",
        created_at="2026-05-15T00:00:00+00:00",
    )


def test_codex_session_context_prompt_does_not_anchor_on_git():
    instruction = _proposal_context_instruction([evidence("user_session_context")])

    assert "session-context packet first" in instruction
    assert "current git status" in instruction
    assert "objective commit diff" not in instruction
    assert "reconcile it against git" not in instruction
    assert "present-tense proof" in instruction
    assert "context_quality_profile" in instruction


def test_kimi_session_context_prompt_does_not_anchor_on_git():
    instruction = _review_context_instruction([evidence("user_session_context")])

    assert "supplied bounded file contents" in instruction
    assert "current git status" in instruction
    assert "git/file evidence" not in instruction
    assert "approval packet" in instruction
    assert "context_quality_profile" in instruction


def test_commit_context_prompt_keeps_commit_diff_language_when_present():
    instruction = _proposal_context_instruction([evidence("objective_commit_diff")])

    assert "objective commit diff when provided" in instruction
    assert "future promise" in instruction


def test_kimi_prompt_prioritizes_review_packet_when_present():
    instruction = _review_context_instruction([evidence("kimi_review_packet")])

    assert "inspect it first" in instruction
    assert "prevent avoidable evidence-request ping-pong" in instruction


def test_codex_omx_prompt_requires_handoff_decision(tmp_path):
    from consensusd.models import Proposal, Run, RunStatus
    from consensusd.runners.subprocess_codex import SubprocessCodexRunner
    from consensusd.settings import Settings

    captured = {}

    class CapturingCodex(SubprocessCodexRunner):
        def _run_codex(self, run, prompt, phase, sandbox="read-only"):
            captured["prompt"] = prompt
            return "Decision: BLOCKED"

    runner = CapturingCodex(Settings.from_env(project_root=tmp_path, db_path=tmp_path / "db.sqlite"))
    run = Run(
        run_id="run_prompt",
        project_root=str(tmp_path),
        objective="review P11B",
        status=RunStatus.OMX_GENERATING,
        mode="approval-gated",
        runner_mode="codex-kimi",
        lease_mode="detached",
        lease_expires_at=None,
        max_rounds=3,
        current_round=1,
        version=1,
        created_at="2026-05-15T00:00:00+00:00",
        updated_at="2026-05-15T00:00:00+00:00",
        locked_at=None,
        error=None,
    )
    proposal = Proposal(
        proposal_id="proposal_prompt",
        run_id=run.run_id,
        round=1,
        runner="codex",
        content="proposal",
        created_at="2026-05-15T00:00:00+00:00",
    )

    runner.generate_omx(run, proposal, [evidence("context_bridge")])

    assert "Ralph Handoff Decision" in captured["prompt"]
    assert "context_bridge" in captured["prompt"]
    assert "authoritative Kimi/Codex negotiation summary" in captured["prompt"]
    assert "CONSENSUSD:PRD_START" in captured["prompt"]
    assert "CONSENSUSD:TEST_SPEC_START" in captured["prompt"]
    assert "proper PRD section" in captured["prompt"]
    assert "proper test spec section" in captured["prompt"]
    assert "Decision: READY" in captured["prompt"]
    assert "Decision: BLOCKED" in captured["prompt"]
    assert "concrete next engineering prompt" in captured["prompt"]


def test_codex_proposal_prompt_requires_draft_prd_and_test_spec(tmp_path):
    from consensusd.models import Run, RunStatus
    from consensusd.runners.subprocess_codex import SubprocessCodexRunner
    from consensusd.settings import Settings

    captured = {}

    class CapturingCodex(SubprocessCodexRunner):
        def _run_codex(self, run, prompt, phase, sandbox="read-only"):
            captured["prompt"] = prompt
            return "proposal"

    runner = CapturingCodex(Settings.from_env(project_root=tmp_path, db_path=tmp_path / "db.sqlite"))
    run = Run(
        run_id="run_prompt",
        project_root=str(tmp_path),
        objective="review P11B",
        status=RunStatus.CODEX_DRAFTING,
        mode="approval-gated",
        runner_mode="codex-kimi",
        lease_mode="detached",
        lease_expires_at=None,
        max_rounds=3,
        current_round=1,
        version=1,
        created_at="2026-05-15T00:00:00+00:00",
        updated_at="2026-05-15T00:00:00+00:00",
        locked_at=None,
        error=None,
    )

    runner.generate_proposal(run, None, [evidence("context_quality_profile")])

    assert "CONSENSUSD:DRAFT_PRD_START" in captured["prompt"]
    assert "CONSENSUSD:DRAFT_TEST_SPEC_START" in captured["prompt"]
    assert "Round 1 is not allowed to be a thin intent note" in captured["prompt"]
    assert "final OMX phase" in captured["prompt"]


def test_kimi_prompt_reviews_draft_bundle(tmp_path):
    from consensusd.models import Proposal, Run, RunStatus
    from consensusd.runners.subprocess_kimi import SubprocessKimiRunner
    from consensusd.settings import Settings

    captured = {}

    class CapturingKimi(SubprocessKimiRunner):
        def _kimi_command(self, run, prompt, session_id):
            captured["prompt"] = prompt
            return [sys.executable, "-c", "print('REVIEW_STATUS: APPROVED')"]

    runner = CapturingKimi(Settings.from_env(project_root=tmp_path, db_path=tmp_path / "db.sqlite"))
    run = Run(
        run_id="run_prompt",
        project_root=str(tmp_path),
        objective="review P11B",
        status=RunStatus.KIMI_REVIEWING,
        mode="approval-gated",
        runner_mode="codex-kimi",
        lease_mode="detached",
        lease_expires_at=None,
        max_rounds=3,
        current_round=1,
        version=1,
        created_at="2026-05-15T00:00:00+00:00",
        updated_at="2026-05-15T00:00:00+00:00",
        locked_at=None,
        error=None,
    )
    proposal = Proposal(
        proposal_id="proposal_prompt",
        run_id=run.run_id,
        round=1,
        runner="codex",
        content="proposal",
        created_at="2026-05-15T00:00:00+00:00",
    )

    runner.review_proposal(run, proposal, [])

    assert "Draft PRD/Test Spec Review" in captured["prompt"]
    assert "CONSENSUSD:DRAFT_PRD_*" in captured["prompt"]
    assert "Critical Issue" in captured["prompt"]


def test_validate_draft_plan_bundle_requires_marked_prd_and_test_spec():
    from consensusd.orchestrator import validate_draft_plan_bundle

    good = """
<!-- CONSENSUSD:DRAFT_PRD_START -->
# Draft PRD - Safety Gate
## Problem
""" + "problem " * 30 + """
## Goals
""" + "goals " * 25 + """
## Non-Goals
""" + "non-goals " * 20 + """
## Requirements
""" + "requirements " * 25 + """
## Acceptance Criteria
""" + "acceptance " * 25 + """
<!-- CONSENSUSD:DRAFT_PRD_END -->
<!-- CONSENSUSD:DRAFT_TEST_SPEC_START -->
# Draft Test Spec - Safety Gate
## Test Matrix
""" + "matrix " * 25 + """
## Negative Tests
""" + "negative " * 25 + """
## Commands
""" + "command " * 20 + """
## Evidence Capture
""" + "evidence " * 20 + """
## Pass/Fail Gates
""" + "pass " * 20 + """
<!-- CONSENSUSD:DRAFT_TEST_SPEC_END -->
"""

    assert validate_draft_plan_bundle(good)["status"] == "OK"
    failed = validate_draft_plan_bundle("# Proposal only")
    assert failed["status"] == "FAILED"
    assert "missing_draft_prd_markers" in failed["failures"]
    assert "missing_draft_test_spec_markers" in failed["failures"]


def test_extracts_resumable_cli_session_ids():
    codex = '{"type":"thread.started","thread_id":"019e2d6e-fe93-71e0-b573-eede780fe508"}'
    kimi = "To resume this session: kimi -r efdbb123-047d-4409-a0a2-5ad493ce70a0"

    assert extract_codex_thread_id(codex) == "019e2d6e-fe93-71e0-b573-eede780fe508"
    assert extract_kimi_session_id(kimi) == "efdbb123-047d-4409-a0a2-5ad493ce70a0"


def test_codex_runner_uses_persisted_resume_session(tmp_path):
    from consensusd.db import Database
    from consensusd.models import Run, RunStatus
    from consensusd.runners.subprocess_codex import CODEX_SESSION_ROLE, SubprocessCodexRunner
    from consensusd.settings import Settings

    db = Database(tmp_path / "db.sqlite")
    db.init()
    run = db.create_run("review", str(tmp_path), "approval-gated", runner_mode="codex")
    db.upsert_runner_session(
        run.run_id,
        CODEX_SESSION_ROLE,
        "019e2d6e-fe93-71e0-b573-eede780fe508",
        source="test",
    )
    runner = SubprocessCodexRunner(Settings.from_env(project_root=tmp_path, db_path=db.path), db=db)

    command = runner._codex_command(run, "prompt", tmp_path / "out.md", "read-only", db.get_runner_session(run.run_id, CODEX_SESSION_ROLE))

    assert command[:3] == ["codex", "exec", "resume"]
    assert "019e2d6e-fe93-71e0-b573-eede780fe508" in command
    assert "--json" in command
    assert "--ephemeral" not in command


def test_kimi_runner_uses_persisted_resume_session(tmp_path):
    from consensusd.db import Database
    from consensusd.runners.subprocess_kimi import KIMI_SESSION_ROLE, SubprocessKimiRunner
    from consensusd.settings import Settings

    db = Database(tmp_path / "db.sqlite")
    db.init()
    run = db.create_run("review", str(tmp_path), "approval-gated", runner_mode="codex-kimi")
    db.upsert_runner_session(
        run.run_id,
        KIMI_SESSION_ROLE,
        "efdbb123-047d-4409-a0a2-5ad493ce70a0",
        source="test",
    )
    runner = SubprocessKimiRunner(Settings.from_env(project_root=tmp_path, db_path=db.path), db=db)

    command = runner._kimi_command(run, "prompt", db.get_runner_session(run.run_id, KIMI_SESSION_ROLE))

    assert command[:2] == ["kimi", "--quiet"]
    assert ["-r", "efdbb123-047d-4409-a0a2-5ad493ce70a0"] == command[command.index("-r") : command.index("-r") + 2]


def test_runners_record_session_ids_from_results(tmp_path):
    from consensusd.db import Database
    from consensusd.models import RunStatus
    from consensusd.runners.subprocess_codex import CODEX_SESSION_ROLE, SubprocessCodexRunner
    from consensusd.runners.subprocess_kimi import KIMI_SESSION_ROLE, SubprocessKimiRunner
    from consensusd.settings import Settings

    db = Database(tmp_path / "db.sqlite")
    db.init()
    run = db.create_run("review", str(tmp_path), "approval-gated", runner_mode="codex-kimi")
    settings = Settings.from_env(project_root=tmp_path, db_path=db.path)

    SubprocessCodexRunner(settings, db=db)._record_session(
        run,
        CommandResult(
            stdout='{"type":"thread.started","thread_id":"019e2d6e-fe93-71e0-b573-eede780fe508"}',
            stderr="",
            returncode=0,
            pid=1,
        ),
        "proposal",
        CODEX_SESSION_ROLE,
    )
    SubprocessKimiRunner(settings, db=db)._record_session(
        run,
        CommandResult(
            stdout="REVIEW_STATUS: APPROVED",
            stderr="To resume this session: kimi -r efdbb123-047d-4409-a0a2-5ad493ce70a0",
            returncode=0,
            pid=2,
        ),
    )

    assert db.get_runner_session(run.run_id, CODEX_SESSION_ROLE) == "019e2d6e-fe93-71e0-b573-eede780fe508"
    assert db.get_runner_session(run.run_id, KIMI_SESSION_ROLE) == "efdbb123-047d-4409-a0a2-5ad493ce70a0"
    event_types = [event.event_type for event in db.transcript(run.run_id).events]
    assert "codex.session.updated" in event_types
    assert "kimi.session.updated" in event_types

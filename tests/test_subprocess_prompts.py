from consensusd.models import Evidence
from consensusd.runners.subprocess_codex import _proposal_context_instruction
from consensusd.runners.subprocess_kimi import _review_context_instruction


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


def test_kimi_session_context_prompt_does_not_anchor_on_git():
    instruction = _review_context_instruction([evidence("user_session_context")])

    assert "supplied bounded file contents" in instruction
    assert "current git status" in instruction
    assert "git/file evidence" not in instruction
    assert "approval packet" in instruction


def test_commit_context_prompt_keeps_commit_diff_language_when_present():
    instruction = _proposal_context_instruction([evidence("objective_commit_diff")])

    assert "objective commit diff when provided" in instruction
    assert "future promise" in instruction


def test_kimi_prompt_prioritizes_review_packet_when_present():
    instruction = _review_context_instruction([evidence("kimi_review_packet")])

    assert "inspect it first" in instruction
    assert "prevent avoidable evidence-request ping-pong" in instruction

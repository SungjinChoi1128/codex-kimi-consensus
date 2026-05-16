from __future__ import annotations

from typing import Optional

from consensusd.db import Database
from consensusd.models import Evidence, Proposal, Review, ReviewDecision, ReviewStatus, Run
from consensusd.runners.subprocess_utils import (
    combined_result_text,
    extract_kimi_session_id,
    phase_artifact_paths,
    run_cancellable_command,
)
from consensusd.settings import Settings

KIMI_SESSION_ROLE = "kimi_reviewer"


class SubprocessKimiRunner:
    """Future Kimi CLI runner with configurable command surface."""

    def __init__(self, settings: Settings, db: Optional[Database] = None):
        self.settings = settings
        self.db = db

    def generate_proposal(self, run: Run, prior_review: Optional[Review], evidence: list[Evidence]) -> str:
        raise NotImplementedError("Kimi does not generate proposals")

    def review_proposal(self, run: Run, proposal: Proposal, evidence: list[Evidence]) -> ReviewDecision:
        context_instruction = _review_context_instruction(evidence)
        prompt = (
            "You are Kimi acting as architect reviewer inside consensusd.\n"
            "Review the Codex proposal against the current repository objective. Do not edit files.\n"
            "You are not the owner or editor of the implementation plan. Codex owns the plan markdown. "
            "Your job is to enrich the shared context, challenge weak assumptions, and provide advisory review material "
            "that Codex must adjudicate.\n"
            f"{context_instruction}\n"
            "Return a critical, evidence-grounded architecture review at the level of an OMX/Kimi plan review. "
            "Review the concrete Codex draft PRD and draft test spec sections, not just the narrative proposal. "
            "If the proposal lacks `CONSENSUSD:DRAFT_PRD_*` or `CONSENSUSD:DRAFT_TEST_SPEC_*` sections, treat that as "
            "a Critical Issue unless the objective is explicitly not a planning-bundle review.\n\n"
            f"Run ID: {run.run_id}\n"
            f"Objective: {run.objective}\n"
            f"Round: {run.current_round}\n"
            f"Project root: {run.project_root}\n\n"
            f"Codex proposal:\n{proposal.content}\n\n"
            f"Evidence:\n{_format_evidence(evidence)}\n\n"
            "Start your final answer with exactly one of:\n"
            "REVIEW_STATUS: APPROVED\n"
            "REVIEW_STATUS: NEEDS_REVISION\n\n"
            "Then use this structure:\n"
            "## Verdict\n"
            "State whether the proposal is approved, approved with reservations, or needs revision.\n\n"
            "## Critical Issues (Must Resolve Before Handoff)\n"
            "Use C1, C2, C3... headings. Each issue must include Problem, Risk, Required resolution, "
            "and concrete evidence from files, commands, transcript, or missing artifacts.\n\n"
            "## Draft PRD/Test Spec Review\n"
            "Review whether the draft PRD and draft test spec are specific, internally consistent, implementable, and safe. "
            "Name missing acceptance criteria, test cases, commands, fixtures, or evidence gates.\n\n"
            "## Major Issues (Should Resolve Before/During Implementation)\n"
            "Use M1, M2... headings for important non-blocking issues.\n\n"
            "## Minor Issues (Polish)\n"
            "Use m1, m2... headings where useful.\n\n"
            "## Governance Checklist\n"
            "Markdown table with PASS/PARTIAL/FAIL rows for safety, scope, approval, evidence, and runner authenticity.\n\n"
            "## Questions For Owner/Architect\n"
            "List any decisions that require human or architect adjudication.\n\n"
            "## Evidence Expectations Before Approval\n"
            "List exact commands/files/artifacts that would prove resolution.\n\n"
            "Use REVIEW_STATUS: NEEDS_REVISION when a Critical Issue must be fixed before Ralph can safely execute. "
            "Do not approve an implementation handoff by calling required fixes 'reservations'. "
            "Only use REVIEW_STATUS: APPROVED with constrained semantics when the remaining critical items are explicitly "
            "accepted non-execution constraints and the plan tells Ralph not to implement yet. "
            "Do not rewrite Codex's plan; write an advisory review that can become context enrichment."
        )
        paths = phase_artifact_paths(run.project_root, run.run_id, "kimi.review", run.current_round)
        live_log_path = paths["live_log"]
        session_id = self.db.get_runner_session(run.run_id, KIMI_SESSION_ROLE) if self.db else None
        command = self._kimi_command(run, prompt, session_id)
        resume_note = f" resume_session={session_id}" if session_id else ""
        print(
            f"[consensusd] run={run.run_id} phase=kimi.review starting "
            f"timeout={_timeout_label(self.settings.subprocess_timeout_sec)} live_log={live_log_path}{resume_note}",
            flush=True,
        )
        result = run_cancellable_command(
            command,
            cwd=run.project_root,
            timeout_sec=self.settings.subprocess_timeout_sec,
            label="kimi subprocess during review",
            live_log_path=live_log_path,
        )
        print(
            f"[consensusd] run={run.run_id} phase=kimi.review completed "
            f"pid={result.pid} returncode={result.returncode} live_log={result.live_log_path}",
            flush=True,
        )
        self._record_session(run, result)
        content = (result.stdout or "").strip()
        if result.returncode != 0:
            details = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"kimi subprocess failed during review: {details}")
        if not content:
            raise RuntimeError("kimi subprocess returned empty review")
        first = content.splitlines()[0].strip().upper()
        status = ReviewStatus.APPROVED if first == "REVIEW_STATUS: APPROVED" else ReviewStatus.NEEDS_REVISION
        return ReviewDecision(status=status, content=content)

    def generate_omx(self, run: Run, consensus_proposal: Proposal, evidence: list[Evidence]) -> str:
        raise NotImplementedError("Kimi does not generate OMX plans")

    def _kimi_command(self, run: Run, prompt: str, session_id: Optional[str]) -> list[str]:
        command = [
            *self.settings.kimi_command,
            "--quiet",
            "-w",
            run.project_root,
        ]
        if session_id:
            command.extend(["-r", session_id])
        command.extend(["-p", prompt])
        return command

    def _record_session(self, run: Run, result) -> None:
        if not self.db:
            return
        session_id = extract_kimi_session_id(combined_result_text(result))
        if not session_id:
            return
        self.db.upsert_runner_session(
            run.run_id,
            KIMI_SESSION_ROLE,
            session_id,
            source="kimi resume hint",
            phase="kimi.review",
            round=run.current_round,
            event_type="kimi.session.updated",
        )


def _format_evidence(evidence: list[Evidence]) -> str:
    if not evidence:
        return "(none)"
    blocks = []
    for item in evidence:
        command = f" command={item.command}" if item.command else ""
        blocks.append(
            f"[round {item.round}] {item.kind} status={item.status}{command}\n"
            f"```text\n{item.output.strip() or '(empty)'}\n```"
        )
    return "\n\n".join(blocks)


def _timeout_label(timeout_sec: int) -> str:
    return "unlimited" if timeout_sec <= 0 else f"{timeout_sec}s"


def _review_context_instruction(evidence: list[Evidence]) -> str:
    kinds = {item.kind for item in evidence}
    packet_instruction = (
        "If `kimi_review_packet` is present, inspect it first as the round's approval packet: current proposal, prior critique, "
        "diff, file contents, raw evidence, and approval checklist are meant to prevent avoidable evidence-request ping-pong. "
    )
    if "user_session_context" in kinds and "objective_commit_diff" not in kinds:
        return (
            packet_instruction +
            "If evidence includes `user_session_context`, treat it as a scoped human/Codex session hint, not as proof. "
            "Use it to understand the user's recent Ralph/reporting flow, then verify claims against the supplied bounded "
            "file contents and explicit evidence. Do not pull review scope from current git status, current HEAD, or "
            "unrelated cleanup/tooling commits unless those git facts are explicitly present in the evidence packet. "
            "If `context_quality_profile` reports `quality_failures`, treat them as missing-evidence blockers unless "
            "the proposal explicitly scopes around them."
        )
    return (
        packet_instruction +
        "Verify claims against the supplied repository evidence. If a specific fact is missing, mark it as missing evidence "
        "instead of widening scope implicitly. If `context_quality_profile` reports `quality_failures`, treat them as "
        "missing-evidence blockers unless the proposal explicitly scopes around them."
    )

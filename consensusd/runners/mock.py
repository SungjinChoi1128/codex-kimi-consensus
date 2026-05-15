from __future__ import annotations

from typing import Optional

from consensusd.models import Evidence, Proposal, Review, ReviewDecision, ReviewStatus, Run


def _diff_stat(evidence: list[Evidence]) -> str:
    for item in evidence:
        if item.kind == "git_diff_stat":
            text = item.output.strip()
            return text if text else "(empty git diff --stat)"
    return "(git diff --stat was not captured)"


class MockCodexRunner:
    def generate_proposal(self, run: Run, prior_review: Optional[Review], evidence: list[Evidence]) -> str:
        diff_stat = _diff_stat(evidence)
        if run.current_round == 1:
            return (
                "# MOCK Codex Proposal Round 1\n\n"
                "**Runner mode:** mock. This is a deterministic transport/state-machine rehearsal, "
                "not a real Codex review of repository semantics.\n\n"
                f"Objective: {run.objective}\n\n"
                f"Project: `{run.project_root}`\n\n"
                "## Captured Repository Evidence\n"
                "```text\n"
                f"{diff_stat}\n"
                "```\n\n"
                "## Draft Review Plan\n"
                "- Treat the supplied Ralph completion summary as a claim set, not proof.\n"
                "- Check the changed files and safety gates before any Ralph handoff.\n"
                "- Keep the handoff approval-gated.\n\n"
                "Known mock gap: this draft does not yet require proof that the approval phrase "
                "and endpoint host validation reject unsafe inputs."
            )
        critique = prior_review.content if prior_review else "No prior critique."
        return (
            "# MOCK Codex Proposal Round 2\n\n"
            "**Runner mode:** mock. This revision only proves the consensus loop shape.\n\n"
            f"Objective: {run.objective}\n\n"
            f"Project: `{run.project_root}`\n\n"
            "## Captured Repository Evidence\n"
            "```text\n"
            f"{diff_stat}\n"
            "```\n\n"
            "## Revised Review Plan\n"
            "- Block Ralph handoff unless approval-phrase validation rejects missing or wrong phrases.\n"
            "- Block Ralph handoff unless URL/host validation rejects non-Revolut hosts instead of normalizing them.\n"
            "- Require targeted tests for the collector guard and skeleton before treating P11B as ready.\n"
            "- Preserve the human approval gate before any Ralph continuation.\n\n"
            f"Addressed Kimi critique:\n{critique}"
        )

    def review_proposal(self, run: Run, proposal: Proposal, evidence: list[Evidence]) -> ReviewDecision:
        raise NotImplementedError("Codex runner does not review proposals")

    def generate_omx(self, run: Run, consensus_proposal: Proposal, evidence: list[Evidence]) -> str:
        return (
            "# MOCK OMX Implementation Plan\n\n"
            "> This plan was produced by deterministic mock runners. It is useful for testing "
            "MCP transport, SQLite durability, event ordering, and approval gating. It is not a "
            "real Codex/Kimi review verdict.\n\n"
            f"Run: `{run.run_id}`\n\n"
            f"Objective: {run.objective}\n\n"
            f"Project: `{run.project_root}`\n\n"
            "## Captured Repository Evidence\n"
            "```text\n"
            f"{_diff_stat(evidence)}\n"
            "```\n\n"
            "## Acceptance Criteria\n"
            "- Treat this run as a mock harness result only.\n"
            "- Before real Ralph handoff, use real Codex/Kimi runners or manual review evidence.\n"
            "- Verify approval-phrase rejection and foreign-host rejection in P11B guard tests.\n"
            "- Keep Ralph handoff approval-gated.\n\n"
            "## Work Plan\n"
            "1. Inspect the P11B guard and collector skeleton changes.\n"
            "2. Add or verify tests for missing approval phrases, wrong approval phrases, and foreign host URLs.\n"
            "3. Re-run the targeted Node tests plus syntax checks.\n"
            "4. Only then consider a real Ralph handoff.\n\n"
            "## Verification\n"
            "- This mock run verified the consensusd workflow plumbing.\n"
            "- It did not verify P11B implementation correctness.\n\n"
            "## Ralph Handoff Packet\n"
            "Do not pass this mock plan to Ralph as a production implementation plan."
        )


class MockKimiRunner:
    def generate_proposal(self, run: Run, prior_review: Optional[Review], evidence: list[Evidence]) -> str:
        raise NotImplementedError("Kimi runner does not generate proposals")

    def review_proposal(self, run: Run, proposal: Proposal, evidence: list[Evidence]) -> ReviewDecision:
        if proposal.round == 1:
            return ReviewDecision(
                status=ReviewStatus.NEEDS_REVISION,
                content=(
                    "Mock Kimi requires revision: before any real Ralph handoff, the plan must demand "
                    "evidence that missing approval phrases are rejected and foreign-host URLs are rejected. "
                    "Evidence expected: git diff --stat, targeted guard tests, and explicit P11B safety notes."
                ),
            )
        return ReviewDecision(
            status=ReviewStatus.APPROVED,
            content=(
                "Approved for mock workflow purposes only. The revised proposal names the critical P11B "
                "evidence gates, but a real Codex/Kimi runner must perform the substantive review."
            ),
        )

    def generate_omx(self, run: Run, consensus_proposal: Proposal, evidence: list[Evidence]) -> str:
        raise NotImplementedError("Kimi runner does not generate OMX plans")

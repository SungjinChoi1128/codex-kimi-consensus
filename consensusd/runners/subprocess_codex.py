from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional

from consensusd.models import Evidence, Proposal, Review, ReviewDecision, Run
from consensusd.runners.subprocess_utils import run_cancellable_command
from consensusd.settings import Settings


class SubprocessCodexRunner:
    """Future Codex CLI runner.

    The command is configurable, but v1 intentionally does not execute it in
    tests or expose shell access to agents.
    """

    def __init__(self, settings: Settings, editable: bool = False):
        self.settings = settings
        self.editable = editable

    def generate_proposal(self, run: Run, prior_review: Optional[Review], evidence: list[Evidence]) -> str:
        prior = prior_review.content if prior_review else "(none)"
        prompt = (
            "You are Codex acting as implementation planner inside consensusd.\n"
            "This is a read-only planning/review pass. Do not edit files. Do not ask questions.\n"
            "Codex is the owner and editor of the implementation plan. Kimi is an advisory reviewer whose output "
            "enriches context and must be adjudicated, not blindly copied as the plan.\n"
            "Time budget: return the proposal quickly. Use the evidence already supplied below as the primary context; "
            "do not run tests, broad static scans, exhaustive grep loops, or long repository audits in this proposal pass. "
            "If more evidence would be useful, name it as missing evidence instead of continuing to inspect indefinitely.\n"
            "Produce a concrete markdown proposal for the current repository/diff at the level of an OMX adjudication artifact.\n\n"
            f"Run ID: {run.run_id}\n"
            f"Objective: {run.objective}\n"
            f"Round: {run.current_round}\n"
            f"Project root: {run.project_root}\n\n"
            f"Prior Kimi review:\n{prior}\n\n"
            f"Evidence:\n{_format_evidence(evidence)}\n\n"
            "Return only markdown using this structure:\n"
            "## Proposal\n"
            "Explain the proposed decision or implementation direction.\n\n"
            "## Evidence Reviewed\n"
            "List exact files, commands, transcript state, and known missing evidence.\n\n"
            "## Adjudication Summary\n"
            "If there is a prior Kimi review, include a markdown table: Kimi item | Codex decision | Plan change | Status. "
            "Respond point-by-point to every C/M/m item and every blocker. If there is no prior review, say initial round.\n\n"
            "## Context Enrichment From Kimi\n"
            "Summarize what Kimi added to the shared context. Keep it advisory and clearly separate from Codex-owned plan decisions.\n\n"
            "## Key Design Decisions\n"
            "List decisions to lock in if Kimi approves.\n\n"
            "## Remaining Disagreement Or Open Questions\n"
            "Name any unresolved decision rather than hiding it.\n\n"
            "## Implementation Plan\n"
            "Concrete file-level steps.\n\n"
            "## Verification Requirements\n"
            "Exact commands/artifacts expected before approval.\n\n"
            "## Safety And Governance Notes\n"
            "Approval gates, non-goals, and forbidden actions.\n\n"
            "If Kimi previously requested revision, do not merely restate the plan; explicitly adjudicate each objection."
        )
        return self._run_codex(run, prompt, "proposal")

    def apply_revision(self, run: Run, prior_review: Review, evidence: list[Evidence]) -> str:
        if not self.editable:
            raise RuntimeError("Codex revision pass requested, but runner is read-only")
        prompt = (
            "You are Codex acting as implementation owner inside consensusd.\n"
            "This is an editable revision pass after Kimi requested changes. You may edit files in the project root "
            "to resolve the review, then return a concise markdown revision report.\n\n"
            "Hard constraints:\n"
            "- Keep changes narrowly scoped to the objective and Kimi's blocking issues.\n"
            "- Do not commit, push, open network resources, read secrets, or perform Ralph handoff.\n"
            "- Do not delete user work to make the diff look clean; explain unrelated dirty state instead.\n"
            "- Prefer small, reviewable repo changes and durable artifacts under `.omx/` when the issue is planning/context evidence.\n"
            "- Run only relevant local verification commands that are already available in the repo.\n\n"
            f"Run ID: {run.run_id}\n"
            f"Objective: {run.objective}\n"
            f"Round to prepare: {run.current_round}\n"
            f"Project root: {run.project_root}\n\n"
            f"Kimi review requiring revision:\n{prior_review.content}\n\n"
            f"Evidence so far:\n{_format_evidence(evidence)}\n\n"
            "Edit the repository now if needed. Then return only markdown using this structure:\n"
            "## Revision Summary\n"
            "What changed and why.\n\n"
            "## Kimi Items Addressed\n"
            "Table: Kimi item | Resolution | Evidence.\n\n"
            "## Files Changed\n"
            "Exact paths changed or intentionally left unchanged.\n\n"
            "## Verification Run\n"
            "Commands run and results. If a command was not run, say why.\n\n"
            "## Remaining Risks\n"
            "Anything Kimi still needs to inspect."
        )
        return self._run_codex(run, prompt, "revision", sandbox="workspace-write")

    def review_proposal(self, run: Run, proposal: Proposal, evidence: list[Evidence]) -> ReviewDecision:
        raise NotImplementedError("Codex does not review proposals")

    def generate_omx(self, run: Run, consensus_proposal: Proposal, evidence: list[Evidence]) -> str:
        prompt = (
            "You are Codex generating the final OMX implementation plan after consensus lock.\n"
            "This is a read-only planning pass. Do not edit files. Do not run destructive commands.\n"
            "Time budget: generate the plan from the consensus transcript and captured evidence; do not run broad scans or tests here.\n"
            "Generate a detailed, production-minded OMX plan that Ralph can execute only after human approval.\n\n"
            f"Run ID: {run.run_id}\n"
            f"Objective: {run.objective}\n"
            f"Project root: {run.project_root}\n\n"
            f"Consensus proposal:\n{consensus_proposal.content}\n\n"
            f"Evidence:\n{_format_evidence(evidence)}\n\n"
            "Return only markdown. Include:\n"
            "- a Codex-owned implementation plan; Codex remains the editor/owner of the plan markdown\n"
            "- a context enrichment bridge section summarizing Kimi's advisory review cycle separately from the plan body\n"
            "- an adjudication table mapping reviewer items to decisions and status\n"
            "- key design decisions now locked in\n"
            "- remaining disagreement or open questions\n"
            "- explicit scope and non-goals\n"
            "- implementation steps\n"
            "- safety/security constraints\n"
            "- verification commands\n"
            "- approval gate before Ralph handoff\n"
            "- known residual risks\n"
            "\nMatch the depth and structure of an OMX plan review bridge, but do not present Kimi as co-author or plan owner. "
            "Kimi enriches context; Codex adjudicates and owns the final plan."
        )
        content = self._run_codex(run, prompt, "omx")
        return "# OMX Plan Generated By Real Codex\n\n" + content.lstrip()

    def _run_codex(self, run: Run, prompt: str, phase: str, sandbox: str = "read-only") -> str:
        with tempfile.TemporaryDirectory(prefix=f"consensusd-codex-{phase}-") as tmp:
            output_path = Path(tmp) / "last-message.md"
            command = [
                *self.settings.codex_command,
                "--ignore-user-config",
                "--ignore-rules",
                "--ephemeral",
                "-C",
                run.project_root,
                "--sandbox",
                sandbox,
                "--output-last-message",
                str(output_path),
                prompt,
            ]
            print(
                f"[consensusd] run={run.run_id} phase=codex.{phase} starting "
                f"timeout={self.settings.subprocess_timeout_sec}s output={output_path}",
                flush=True,
            )
            result = run_cancellable_command(
                command,
                cwd=run.project_root,
                timeout_sec=self.settings.subprocess_timeout_sec,
                label=f"codex subprocess during {phase}",
            )
            print(
                f"[consensusd] run={run.run_id} phase=codex.{phase} completed "
                f"pid={result.pid} returncode={result.returncode}",
                flush=True,
            )
            output = output_path.read_text() if output_path.exists() else result.stdout
            if result.returncode != 0:
                details = (result.stderr or result.stdout or "").strip()
                raise RuntimeError(f"codex subprocess failed during {phase}: {details}")
            output = output.strip()
            if not output:
                raise RuntimeError(f"codex subprocess returned empty output during {phase}")
            return output


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

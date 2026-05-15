from __future__ import annotations

import fcntl
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .db import Database
from .models import Proposal, ReviewStatus, Run, RunStatus
from .runners.base import AgentRunner, RevisionRunner
from .runners.mock import MockCodexRunner, MockKimiRunner
from .runners.subprocess_codex import SubprocessCodexRunner
from .runners.subprocess_kimi import SubprocessKimiRunner
from .settings import Settings
from .state_machine import is_terminal


def runner_name(runner: AgentRunner) -> str:
    return runner.__class__.__name__


def slugify(text: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug or fallback)[:80]


class Orchestrator:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        codex_runner: Optional[AgentRunner] = None,
        kimi_runner: Optional[AgentRunner] = None,
    ):
        self.db = db
        self.settings = settings
        self.codex_runner = codex_runner
        self.kimi_runner = kimi_runner
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock_path = self.db.path.parent / "orchestrator.lock"

    def _default_runners(self, runner_mode: str) -> tuple[AgentRunner, AgentRunner]:
        if runner_mode == "mock":
            return MockCodexRunner(), MockKimiRunner()
        if runner_mode == "codex":
            return SubprocessCodexRunner(self.settings), MockKimiRunner()
        if runner_mode == "codex-kimi":
            return SubprocessCodexRunner(self.settings), SubprocessKimiRunner(self.settings)
        if runner_mode == "codex-kimi-edit":
            return SubprocessCodexRunner(self.settings, editable=True), SubprocessKimiRunner(self.settings)
        raise ValueError(f"unsupported runner mode: {runner_mode}")

    def _codex_for(self, run: Run) -> AgentRunner:
        if self.codex_runner:
            return self.codex_runner
        return self._default_runners(run.runner_mode)[0]

    def _kimi_for(self, run: Run) -> AgentRunner:
        if self.kimi_runner:
            return self.kimi_runner
        return self._default_runners(run.runner_mode)[1]

    def start_background(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self.run_forever, name="consensusd-orchestrator", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def run_forever(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.settings.poll_interval)

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def tick_until_idle(self, max_ticks: int = 100) -> None:
        with self.exclusive():
            self._tick_until_idle_unlocked(max_ticks)

    def _tick_until_idle_unlocked(self, max_ticks: int = 100) -> None:
        for _ in range(max_ticks):
            before = [(run.run_id, run.status, run.version) for run in self.db.list_active_runs()]
            self._tick_unlocked()
            after_runs = self.db.list_active_runs()
            after = [(run.run_id, run.status, run.version) for run in after_runs]
            if before == after and all(run.status == RunStatus.AWAITING_HUMAN_APPROVAL for run in after_runs):
                return
            if not after_runs:
                return

    def tick(self) -> None:
        with self.exclusive():
            self._tick_unlocked()

    def _tick_unlocked(self) -> None:
        for run in self.db.list_active_runs():
            try:
                self.advance(run)
            except Exception as exc:
                latest = self.db.get_run(run.run_id)
                if not is_terminal(latest.status):
                    self._ensure_context_bridge(latest)
                    self.db.transition_run(latest.run_id, RunStatus.FAILED, expected_version=latest.version, error=str(exc))

    def advance(self, run: Run) -> None:
        if run.status == RunStatus.INIT:
            self._record_initial_repo_evidence(run)
            self.db.transition_run(run.run_id, RunStatus.AWAITING_CODEX_PROPOSAL, expected_version=run.version)
            return
        if run.status == RunStatus.AWAITING_CODEX_PROPOSAL:
            if run.current_round > run.max_rounds:
                self._ensure_context_bridge(run)
                self.db.transition_run(
                    run.run_id,
                    RunStatus.FAILED,
                    expected_version=run.version,
                    error=f"max rounds exceeded ({run.max_rounds})",
                )
                return
            self.db.transition_run(run.run_id, RunStatus.CODEX_DRAFTING, expected_version=run.version)
            return
        if run.status == RunStatus.CODEX_DRAFTING:
            prior_review = self.db.latest_review(run.run_id)
            runner = self._codex_for(run)
            if prior_review and self._revision_enabled(run, runner):
                revision = self._revision_runner(runner).apply_revision(
                    run,
                    prior_review,
                    self.db.list_evidence(run.run_id),
                )
                self.db.add_evidence(
                    run.run_id,
                    run.current_round,
                    "codex_revision",
                    "OK",
                    revision,
                    command=runner_name(runner),
                )
                self._record_repo_stat(run, "git_diff_stat_after_codex_revision")
            content = runner.generate_proposal(run, prior_review, self.db.list_evidence(run.run_id))
            self.db.add_proposal(run.run_id, run.current_round, content, runner=runner_name(runner))
            self._record_repo_stat(run, "git_diff_stat_before_kimi_review")
            latest = self.db.get_run(run.run_id)
            self.db.transition_run(run.run_id, RunStatus.AWAITING_KIMI_REVIEW, expected_version=latest.version)
            return
        if run.status == RunStatus.AWAITING_KIMI_REVIEW:
            self.db.transition_run(run.run_id, RunStatus.KIMI_REVIEWING, expected_version=run.version)
            return
        if run.status == RunStatus.KIMI_REVIEWING:
            proposal = self.db.get_proposal(run.run_id, run.current_round)
            runner = self._kimi_for(run)
            decision = runner.review_proposal(run, proposal, self.db.list_evidence(run.run_id))
            self.db.add_review(run.run_id, run.current_round, decision.status, decision.content, runner=runner_name(runner))
            latest = self.db.get_run(run.run_id)
            if decision.status == ReviewStatus.APPROVED:
                self.db.transition_run(run.run_id, RunStatus.CONSENSUS_LOCKED, expected_version=latest.version)
            else:
                self.db.transition_run(
                    run.run_id,
                    RunStatus.REVISION_REQUESTED,
                    expected_version=latest.version,
                    increment_round=True,
                )
            return
        if run.status == RunStatus.REVISION_REQUESTED:
            self.db.transition_run(run.run_id, RunStatus.AWAITING_CODEX_PROPOSAL, expected_version=run.version)
            return
        if run.status == RunStatus.CONSENSUS_LOCKED:
            self._ensure_context_bridge(run)
            self.db.transition_run(run.run_id, RunStatus.AWAITING_OMX, expected_version=run.version)
            return
        if run.status == RunStatus.AWAITING_OMX:
            self.db.transition_run(run.run_id, RunStatus.OMX_GENERATING, expected_version=run.version)
            return
        if run.status == RunStatus.OMX_GENERATING:
            proposal = self.db.latest_proposal(run.run_id)
            if not proposal:
                raise RuntimeError("cannot generate OMX without a proposal")
            runner = self._codex_for(run)
            content = runner.generate_omx(run, proposal, self.db.list_evidence(run.run_id))
            plan_path = self._write_omx_plan(run, content)
            self.db.add_omx_plan(run.run_id, content, runner=runner_name(runner), path=plan_path)
            latest = self.db.get_run(run.run_id)
            self.db.transition_run(run.run_id, RunStatus.OMX_GENERATED, expected_version=latest.version)
            return
        if run.status == RunStatus.OMX_GENERATED:
            self.db.transition_run(run.run_id, RunStatus.AWAITING_HUMAN_APPROVAL, expected_version=run.version)
            return
        if run.status == RunStatus.RALPH_HANDOFF_APPROVED:
            transcript = self.db.transcript(run.run_id)
            packet = {
                "run_id": run.run_id,
                "objective": run.objective,
                "omx_plan": transcript.omx_plans[-1].content if transcript.omx_plans else "",
                "context_bridge_path": transcript.context_bridges[-1].path if transcript.context_bridges else "",
                "context_bridge": transcript.context_bridges[-1].content if transcript.context_bridges else "",
                "mode": "mock-ralph",
            }
            self.db.add_handoff(run.run_id, packet, status="mock-complete")
            latest = self.db.get_run(run.run_id)
            self.db.transition_run(run.run_id, RunStatus.RALPH_HANDOFF_COMPLETE, expected_version=latest.version)

    def _record_initial_repo_evidence(self, run: Run) -> None:
        existing = self.db.list_evidence(run.run_id)
        if any(item.kind == "git_diff_stat" for item in existing):
            return
        status, output = run_fixed_git_command(run.project_root, "git diff --stat")
        self.db.add_evidence(
            run.run_id,
            run.current_round,
            "git_diff_stat",
            status,
            output or "(no working-tree diff reported by git diff --stat)",
            command="git diff --stat",
        )

    def _record_repo_stat(self, run: Run, kind: str) -> None:
        status, output = run_fixed_git_command(run.project_root, "git diff --stat")
        self.db.add_evidence(
            run.run_id,
            run.current_round,
            kind,
            status,
            output or "(no working-tree diff reported by git diff --stat)",
            command="git diff --stat",
        )

    def _revision_enabled(self, run: Run, runner: AgentRunner) -> bool:
        return run.runner_mode.endswith("-edit") and hasattr(runner, "apply_revision")

    def _revision_runner(self, runner: AgentRunner) -> RevisionRunner:
        return runner  # type: ignore[return-value]

    def _ensure_context_bridge(self, run: Run) -> None:
        transcript = self.db.transcript(run.run_id)
        if transcript.context_bridges:
            return
        path = self._context_bridge_path(run)
        content = build_context_bridge_markdown(transcript, path)
        bridge_path = Path(path)
        bridge_path.parent.mkdir(parents=True, exist_ok=True)
        bridge_path.write_text(content)
        self.db.add_context_bridge(run.run_id, str(bridge_path), content)

    def _context_bridge_path(self, run: Run) -> str:
        plans_dir = Path(run.project_root) / ".omx" / "plans"
        slug = slugify(run.objective, "consensus-review")
        return str(plans_dir / f"kimi-codex-context-bridge-{slug}-{run.run_id}.md")

    def _write_omx_plan(self, run: Run, content: str) -> str:
        plans_dir = Path(run.project_root) / ".omx" / "plans"
        slug = slugify(run.objective, "consensus-review")
        path = plans_dir / f"consensus-omx-plan-{slug}-{run.run_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return str(path)


def build_context_bridge_markdown(transcript, path: str) -> str:
    run = transcript.run
    lines: list[str] = [
        f"# Kimi-Codex Context Bridge — {run.objective}",
        "",
        f"**Run ID:** `{run.run_id}`  ",
        f"**Runner mode:** `{run.runner_mode}`  ",
        f"**Status:** `{run.status.value}`  ",
        f"**Current round:** `{run.current_round}`  ",
        f"**Bridge path:** `{path}`",
        "",
        "---",
        "",
        "## What Happened",
        "",
    ]
    for proposal in transcript.proposals:
        lines.append(f"- **Round {proposal.round}:** Codex proposal by `{proposal.runner}`.")
        review = next((item for item in transcript.reviews if item.round == proposal.round), None)
        if review:
            lines.append(f"- **Round {review.round}:** Kimi review by `{review.runner}` -> `{review.status.value}`.")
    if transcript.omx_plans:
        lines.append(f"- **Final OMX:** generated by `{transcript.omx_plans[-1].runner}`.")
    lines.extend(
        [
            "",
            "## Review Cycle Summary",
            "",
            "| Round | Codex runner | Kimi runner | Kimi status | Codex proposal summary | Kimi review summary |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for proposal in transcript.proposals:
        review = next((item for item in transcript.reviews if item.round == proposal.round), None)
        lines.append(
            "| "
            f"{proposal.round} | "
            f"`{proposal.runner}` | "
            f"`{review.runner if review else 'pending'}` | "
            f"`{review.status.value if review else 'pending'}` | "
            f"{markdown_cell(summarize(proposal.content))} | "
            f"{markdown_cell(summarize(review.content) if review else 'pending')} |"
        )
    lines.extend(["", "## Adjudication Summary", ""])
    for review in transcript.reviews:
        lines.extend(
            [
                f"### Round {review.round} Kimi Review — `{review.status.value}`",
                "",
                excerpt(review.content, 2400),
                "",
                f"### Round {review.round} Codex Response",
                "",
                excerpt(next((p.content for p in transcript.proposals if p.round == review.round + 1), "(final round; see OMX plan)") or "", 1800),
                "",
            ]
        )
    lines.extend(["## Key Design Decisions Now Locked In", ""])
    approved = [review for review in transcript.reviews if review.status == ReviewStatus.APPROVED]
    if approved:
        lines.append("The final approved Kimi review accepted the Codex-owned plan with these context constraints:")
        lines.append("")
        lines.append(excerpt(approved[-1].content, 2200))
    else:
        lines.append("No approving Kimi review is recorded yet.")
    lines.extend(["", "## Remaining Disagreement Or Open Questions", ""])
    latest_review = transcript.reviews[-1] if transcript.reviews else None
    if latest_review and latest_review.status == ReviewStatus.APPROVED:
        lines.append("No blocking disagreement remains in the recorded consensus loop. Non-blocking cautions remain advisory context.")
    elif latest_review:
        lines.append("Blocking disagreement remains; latest Kimi review requested revision.")
    else:
        lines.append("No Kimi review is recorded.")
    lines.extend(["", "## Evidence Captured", ""])
    for item in transcript.evidence:
        lines.extend(
            [
                f"### {item.kind} — `{item.status}`",
                "",
                f"Command: `{item.command or 'n/a'}`",
                "",
                "```text",
                item.output.strip() or "(empty)",
                "```",
                "",
            ]
        )
    lines.extend(["## Files And Artifacts", ""])
    lines.append(f"- Context bridge: `{path}`")
    for plan in transcript.omx_plans:
        lines.append(f"- OMX plan `{plan.omx_id}` generated by `{plan.runner}` at `{plan.created_at}`")
    lines.extend(
        [
            "",
            "## Ralph Handoff Context",
            "",
            "Ralph should treat this bridge as context enrichment, not as a replacement for the Codex-owned OMX plan. "
            "Codex owns the implementation plan; Kimi's material is advisory review context that Codex adjudicated.",
            "",
            "No Ralph handoff is authorized unless the run is explicitly approved by the human owner.",
            "",
            "---",
            "",
            "*This bridge is generated by consensusd from the durable SQLite transcript. It is an audit/context artifact and does not authorize implementation by itself.*",
        ]
    )
    return "\n".join(lines) + "\n"


def summarize(text: str, limit: int = 220) -> str:
    compact = " ".join(text.strip().split())
    return compact[:limit].rstrip() + ("..." if len(compact) > limit else "")


def excerpt(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "\n\n... excerpt truncated; see SQLite transcript for full text."


def markdown_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def run_fixed_git_command(project_root: str, command: str) -> tuple[str, str]:
    allowed = {
        "git diff --stat": ["git", "diff", "--stat"],
        "git diff": ["git", "diff"],
    }
    if command not in allowed:
        raise ValueError("unsupported fixed verification command")
    result = subprocess.run(
        allowed[command],
        cwd=Path(project_root),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    status = "OK" if result.returncode == 0 else "FAILED"
    return status, result.stdout + result.stderr


def changed_files_from_diff_stat(stat_output: str) -> list[str]:
    files: list[str] = []
    for line in stat_output.splitlines():
        if "|" not in line:
            continue
        files.append(line.split("|", 1)[0].strip())
    return files

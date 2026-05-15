from __future__ import annotations

import fcntl
import hashlib
import json
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional, TypeVar

from .db import Database
from .models import Proposal, ReviewStatus, Run, RunStatus
from .runners.base import AgentRunner, RevisionRunner, RunnerCancelled, reset_cancel_check, set_cancel_check
from .runners.mock import MockCodexRunner, MockKimiRunner
from .runners.subprocess_codex import SubprocessCodexRunner
from .runners.subprocess_kimi import SubprocessKimiRunner
from .runners.subprocess_utils import phase_artifact_paths
from .settings import Settings
from .state_machine import is_terminal

T = TypeVar("T")


def runner_name(runner: AgentRunner) -> str:
    return runner.__class__.__name__


def slugify(text: str, fallback: str, limit: int = 80) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug or fallback)[:limit]


def compact_artifact_name(prefix: str, run: Run) -> str:
    slug = slugify(run.objective, "consensus-review", limit=40)
    return f"{prefix}-{run.run_id[-8:]}-{slug}.md"


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
                if self._cancel_if_lease_expired(run.run_id):
                    continue
                self.advance(run)
            except RunnerCancelled:
                latest = self.db.get_run(run.run_id)
                if not is_terminal(latest.status):
                    self.db.transition_run(latest.run_id, RunStatus.CANCELLED, expected_version=latest.version)
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
                ensure_git_repo_for_editable(run.project_root)
                before = repo_change_snapshot(run.project_root)
                revision = self._run_with_phase_events(
                    run,
                    "codex.revision",
                    runner,
                    lambda: self._revision_runner(runner).apply_revision(
                        run,
                        prior_review,
                        self.db.list_evidence(run.run_id),
                    ),
                )
                after = repo_change_snapshot(run.project_root)
                change_summary = summarize_editable_changes(before, after, self.settings)
                self.db.add_evidence(
                    run.run_id,
                    run.current_round,
                    "codex_revision",
                    "OK",
                    revision,
                    command=runner_name(runner),
                )
                self.db.add_evidence(
                    run.run_id,
                    run.current_round,
                    "editable_change_summary",
                    "FAILED" if change_summary["violations"] else "OK",
                    json.dumps(change_summary, indent=2, sort_keys=True),
                    command="git status --porcelain + git diff",
                )
                self._phase_event(run, "codex.revision.audited", runner=runner_name(runner), **change_summary)
                if change_summary["violations"]:
                    raise RuntimeError(f"editable revision touched disallowed paths: {', '.join(change_summary['violations'])}")
                self._record_repo_stat(run, "git_diff_stat_after_codex_revision")
            content = self._run_with_phase_events(
                run,
                "codex.proposal",
                runner,
                lambda: runner.generate_proposal(run, prior_review, self.db.list_evidence(run.run_id)),
            )
            if is_terminal(self.db.get_run(run.run_id).status):
                return
            self.db.add_proposal(run.run_id, run.current_round, content, runner=runner_name(runner))
            self._record_repo_stat(run, "git_diff_stat_before_kimi_review")
            self._record_kimi_review_packet(run)
            latest = self.db.get_run(run.run_id)
            self.db.transition_run(run.run_id, RunStatus.AWAITING_KIMI_REVIEW, expected_version=latest.version)
            return
        if run.status == RunStatus.AWAITING_KIMI_REVIEW:
            self.db.transition_run(run.run_id, RunStatus.KIMI_REVIEWING, expected_version=run.version)
            return
        if run.status == RunStatus.KIMI_REVIEWING:
            proposal = self.db.get_proposal(run.run_id, run.current_round)
            runner = self._kimi_for(run)
            decision = self._run_with_phase_events(
                run,
                "kimi.review",
                runner,
                lambda: runner.review_proposal(run, proposal, self.db.list_evidence(run.run_id)),
            )
            if is_terminal(self.db.get_run(run.run_id).status):
                return
            self._phase_event(run, "kimi.review.verdict", runner=runner_name(runner), review_status=decision.status.value)
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
            content = self._run_with_phase_events(
                run,
                "codex.omx",
                runner,
                lambda: runner.generate_omx(run, proposal, self.db.list_evidence(run.run_id)),
            )
            if is_terminal(self.db.get_run(run.run_id).status):
                return
            plan_path = self._write_omx_plan(run, content)
            self._phase_event(run, "codex.omx.written", runner=runner_name(runner), path=plan_path)
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
        session_context = "\n\n".join(item.output for item in existing if item.kind == "user_session_context")
        session_context_anchored = bool(session_context) and not extract_commit_refs(run.objective)
        if session_context_anchored:
            self.db.add_evidence(
                run.run_id,
                run.current_round,
                "context_scope_note",
                "OK",
                (
                    "This review is anchored to control-surface `user_session_context` because the objective did not name "
                    "an explicit commit. consensusd intentionally omits git status, diff, and HEAD commit evidence from the "
                    "agent prompt to avoid cross-session contamination. Bounded repo file context may still be supplied so "
                    "Codex/Kimi can reconcile the session summary against actual files."
                ),
                command="consensusd session-context anchored scope",
            )
            self._record_deep_context_evidence(run, session_context=session_context)
            return
        for kind, command, empty_message in initial_git_evidence_commands(run.objective):
            status, output = run_fixed_git_command(run.project_root, command)
            self.db.add_evidence(
                run.run_id,
                run.current_round,
                kind,
                status,
                output or empty_message,
                command=command,
            )
        self._record_deep_context_evidence(run)

    def _record_deep_context_evidence(self, run: Run, session_context: str | None = None) -> None:
        if not is_git_repo(run.project_root):
            return
        for item in build_deep_context_evidence(run.project_root, run.objective, session_context=session_context):
            self.db.add_evidence(
                run.run_id,
                run.current_round,
                item["kind"],
                item["status"],
                item["output"],
                command=item.get("command"),
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

    def _record_kimi_review_packet(self, run: Run) -> None:
        if any(item.kind == "kimi_review_packet" and item.round == run.current_round for item in self.db.list_evidence(run.run_id)):
            return
        path = self._kimi_review_packet_path(run)
        content = build_kimi_review_packet_markdown(self.db.transcript(run.run_id), path)
        packet_path = Path(path)
        packet_path.parent.mkdir(parents=True, exist_ok=True)
        packet_path.write_text(content)
        self.db.add_evidence(
            run.run_id,
            run.current_round,
            "kimi_review_packet",
            "OK",
            cap_text(f"Packet path: {path}\n\n{content}", DEEP_CONTEXT_CHAR_LIMIT),
            command="consensusd generated Kimi review packet",
        )

    def _kimi_review_packet_path(self, run: Run) -> str:
        context_dir = Path(run.project_root) / ".omx" / "context"
        return str(context_dir / compact_artifact_name("kimi-review-packet", run))

    def _revision_enabled(self, run: Run, runner: AgentRunner) -> bool:
        return run.runner_mode.endswith("-edit") and hasattr(runner, "apply_revision")

    def _revision_runner(self, runner: AgentRunner) -> RevisionRunner:
        return runner  # type: ignore[return-value]

    def _phase_event(self, run: Run, event_type: str, **payload) -> None:
        self.db.add_event(
            run.run_id,
            event_type,
            {
                "round": run.current_round,
                "status": run.status.value,
                **payload,
            },
        )

    def _run_with_phase_events(self, run: Run, event_prefix: str, runner: AgentRunner, call: Callable[[], T]) -> T:
        started = time.monotonic()
        label = runner_name(runner)
        self._phase_event(run, f"{event_prefix}.started", runner=label)
        result: list[T] = []
        errors: list[BaseException] = []

        def target() -> None:
            token = set_cancel_check(lambda: self._runner_should_cancel(run.run_id))
            try:
                result.append(call())
            except BaseException as exc:  # noqa: BLE001 - re-raised after heartbeat loop
                errors.append(exc)
            finally:
                reset_cancel_check(token)

        worker = threading.Thread(target=target, daemon=True)
        worker.start()
        heartbeats = 0
        interval = self.settings.heartbeat_interval_sec
        join_interval = interval if interval > 0 else 0.25
        cancellation_recorded = False
        try:
            while worker.is_alive():
                worker.join(timeout=join_interval)
                if worker.is_alive() and self._cancel_if_lease_expired(run.run_id):
                    elapsed = round(time.monotonic() - started, 3)
                    self._phase_event(
                        run,
                        f"{event_prefix}.cancelled",
                        runner=label,
                        elapsed_seconds=elapsed,
                        heartbeats=heartbeats,
                        reason="attached client heartbeat expired",
                    )
                    cancellation_recorded = True
                    worker.join(timeout=min(join_interval, 1.0))
                if worker.is_alive() and interval > 0:
                    heartbeats += 1
                    self._phase_event(
                        run,
                        f"{event_prefix}.heartbeat",
                        runner=label,
                        elapsed_seconds=round(time.monotonic() - started, 3),
                        heartbeat=heartbeats,
                        **self._heartbeat_progress_payload(run, event_prefix),
                    )
        except KeyboardInterrupt:
            self._cancel_run_if_active(run.run_id, error="interrupted by user")
            worker.join(timeout=5)
            elapsed = round(time.monotonic() - started, 3)
            self._phase_event(run, f"{event_prefix}.cancelled", runner=label, elapsed_seconds=elapsed, heartbeats=heartbeats)
            raise

        elapsed = round(time.monotonic() - started, 3)
        latest_status = self.db.get_run(run.run_id).status
        if errors:
            suffix = "cancelled" if isinstance(errors[0], RunnerCancelled) or is_terminal(latest_status) else "failed"
            if not cancellation_recorded or suffix != "cancelled":
                self._phase_event(run, f"{event_prefix}.{suffix}", runner=label, elapsed_seconds=elapsed, heartbeats=heartbeats)
            raise errors[0]
        if is_terminal(latest_status):
            if not cancellation_recorded:
                self._phase_event(run, f"{event_prefix}.cancelled", runner=label, elapsed_seconds=elapsed, heartbeats=heartbeats)
            raise RunnerCancelled(f"{event_prefix} stopped because run is {latest_status.value}")
        self._phase_event(run, f"{event_prefix}.completed", runner=label, elapsed_seconds=elapsed, heartbeats=heartbeats)
        return result[0]

    def _runner_should_cancel(self, run_id: str) -> bool:
        if self._cancel_if_lease_expired(run_id):
            return True
        return is_terminal(self.db.get_run(run_id).status)

    def _cancel_if_lease_expired(self, run_id: str) -> bool:
        latest = self.db.get_run(run_id)
        if is_terminal(latest.status):
            return True
        if latest.status == RunStatus.AWAITING_HUMAN_APPROVAL:
            return False
        if latest.lease_mode != "attached" or not latest.lease_expires_at:
            return False
        if parse_utc_iso(latest.lease_expires_at) > datetime.now(timezone.utc):
            return False
        self.db.add_event(
            run_id,
            "run.lease_expired",
            {
                "lease_mode": latest.lease_mode,
                "lease_expires_at": latest.lease_expires_at,
                "status": latest.status.value,
            },
        )
        self.db.transition_run(
            run_id,
            RunStatus.CANCELLED,
            expected_version=latest.version,
            error="attached client heartbeat expired",
        )
        return True

    def _cancel_run_if_active(self, run_id: str, error: str | None = None) -> None:
        latest = self.db.get_run(run_id)
        if is_terminal(latest.status):
            return
        self.db.transition_run(run_id, RunStatus.CANCELLED, expected_version=latest.version, error=error)

    def _heartbeat_progress_payload(self, run: Run, event_prefix: str) -> dict[str, object]:
        payload: dict[str, object] = {}
        if event_prefix.startswith(("codex.", "kimi.")):
            paths = phase_artifact_paths(run.project_root, run.run_id, event_prefix, run.current_round)
            live_log = paths["live_log"]
            last_message = paths["last_message"]
            payload.update(
                {
                    "live_log_path": str(live_log),
                    "live_log_bytes": file_size(live_log),
                    "last_message_path": str(last_message),
                    "last_message_exists": last_message.exists(),
                    "last_message_bytes": file_size(last_message),
                }
            )
        if event_prefix != "codex.revision":
            return payload
        payload["changed_files"] = git_changed_files(run.project_root)
        return payload

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
        return str(plans_dir / compact_artifact_name("context-bridge", run))

    def _write_omx_plan(self, run: Run, content: str) -> str:
        plans_dir = Path(run.project_root) / ".omx" / "plans"
        path = plans_dir / compact_artifact_name("consensus-omx", run)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return str(path)


def parse_utc_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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


def build_kimi_review_packet_markdown(transcript, path: str) -> str:
    run = transcript.run
    root = Path(run.project_root)
    changed = git_changed_files(run.project_root)
    relevant_paths = kimi_review_packet_paths(transcript, changed)
    lines: list[str] = [
        f"# Kimi Review Evidence Packet — Round {run.current_round}",
        "",
        f"**Run ID:** `{run.run_id}`  ",
        f"**Round:** `{run.current_round}`  ",
        f"**Status at packet build:** `{run.status.value}`  ",
        f"**Packet path:** `{path}`",
        "",
        "This packet is generated before Kimi review so Kimi can inspect evidence directly instead of requesting it in later rounds.",
        "",
        "## Current Codex Proposal",
        "",
        excerpt(transcript.proposals[-1].content, 16_000) if transcript.proposals else "(no proposal recorded)",
        "",
        "## Latest Kimi Review Being Addressed",
        "",
        excerpt(transcript.reviews[-1].content, 12_000) if transcript.reviews else "(initial review round)",
        "",
        "## Git Diff Stat",
        "",
        fenced(run_fixed_git_command(run.project_root, "git diff --stat")[1] or "(no working-tree diff reported by git diff --stat)"),
        "",
        "## Full Current Git Diff",
        "",
        fenced(cap_text(run_fixed_git_command(run.project_root, "git diff")[1] or "(empty diff)", DEEP_CONTEXT_CHAR_LIMIT)),
        "",
        "## Current Changed Files",
        "",
    ]
    if changed:
        lines.extend(f"- `{item}`" for item in changed)
    else:
        lines.append("- (no changed files reported by git status)")
    lines.extend(["", "## Review-Relevant File Contents", ""])
    if relevant_paths:
        lines.append(read_context_files(root, relevant_paths))
    else:
        lines.append("(no bounded review-relevant files found)")
    lines.extend(["", "## Raw Verification And Runner Evidence", ""])
    for item in transcript.evidence:
        if item.round != run.current_round:
            continue
        if item.kind in {"kimi_review_packet", "deep_context_file_contents"}:
            continue
        lines.extend(
            [
                f"### {item.kind} — `{item.status}`",
                "",
                f"Command: `{item.command or 'n/a'}`",
                "",
                fenced(excerpt(item.output, 20_000)),
                "",
            ]
        )
    lines.extend(
        [
            "## Kimi Approval Gate Checklist",
            "",
            "- Verify the proposal against the actual diff and file contents above.",
            "- Do not accept narrative claims without matching source, artifact, or raw command evidence.",
            "- If approval is blocked only by missing evidence, name the exact evidence field or path that should be added.",
            "- If all critical issues are resolved, return `REVIEW_STATUS: APPROVED` with advisory reservations separated from blockers.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def kimi_review_packet_paths(transcript, changed_files: list[str]) -> list[str]:
    paths: list[str] = []
    paths.extend(changed_files)
    for item in transcript.evidence:
        if item.kind in {"deep_context_file_inventory", "user_session_context", "kimi_review_packet"}:
            paths.extend(extract_context_file_paths(item.output))
    for proposal in transcript.proposals[-2:]:
        paths.extend(extract_context_file_paths(proposal.content))
    for review in transcript.reviews[-2:]:
        paths.extend(extract_context_file_paths(review.content))
    root = Path(transcript.run.project_root)
    return unique_preserve_order(path for path in paths if safe_context_file(root, path))[:DEEP_CONTEXT_FILE_LIMIT]


def fenced(text: str) -> str:
    return f"```text\n{text.strip() or '(empty)'}\n```"


def summarize(text: str, limit: int = 220) -> str:
    compact = " ".join(text.strip().split())
    return compact[:limit].rstrip() + ("..." if len(compact) > limit else "")


def excerpt(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "\n\n... excerpt truncated; see SQLite transcript for full text."


def markdown_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def repo_change_snapshot(project_root: str) -> dict[str, str]:
    if not is_git_repo(project_root):
        return {}
    files = git_changed_files(project_root)
    return {file: git_diff_hash(project_root, file) for file in files}


def summarize_editable_changes(before: dict[str, str], after: dict[str, str], settings: Settings) -> dict[str, object]:
    changed_files = sorted(after)
    revision_changed_files = sorted(file for file in set(before) | set(after) if before.get(file) != after.get(file))
    violations = [file for file in revision_changed_files if editable_path_violation(file, settings)]
    return {
        "changed_files": changed_files,
        "revision_changed_files": revision_changed_files,
        "violations": violations,
        "allowed_paths": list(settings.editable_allowed_paths),
        "denied_paths": list(settings.editable_denied_paths),
    }


def editable_path_violation(path: str, settings: Settings) -> bool:
    normalized = path.replace("\\", "/").lstrip("/")
    denied = tuple(item.replace("\\", "/").lstrip("/") for item in settings.editable_denied_paths)
    if any(path_matches_rule(normalized, item) for item in denied):
        return True
    allowed = tuple(item.replace("\\", "/").lstrip("/") for item in settings.editable_allowed_paths)
    if allowed and not any(path_matches_rule(normalized, item) for item in allowed):
        return True
    return False


def path_matches_rule(path: str, rule: str) -> bool:
    rule = rule.lstrip("/")
    if not rule:
        return False
    if rule.endswith("/"):
        prefix = rule
        return path == prefix.rstrip("/") or path.startswith(prefix)
    if rule.endswith("."):
        return path.startswith(rule)
    return path == rule


def git_changed_files(project_root: str) -> list[str]:
    if not is_git_repo(project_root):
        return []
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z"],
        cwd=Path(project_root),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    files: list[str] = []
    entries = [item for item in result.stdout.split("\0") if item]
    index = 0
    while index < len(entries):
        entry = entries[index]
        status = entry[:2]
        path = entry[3:]
        if path:
            files.append(path)
        index += 2 if status[0] in {"R", "C"} or status[1] in {"R", "C"} else 1
    return sorted(set(files))


def is_git_repo(project_root: str) -> bool:
    root = Path(project_root)
    if not root.exists():
        return False
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def ensure_git_repo_for_editable(project_root: str) -> None:
    root = Path(project_root)
    if not root.exists():
        raise RuntimeError(f"editable mode requires an existing git worktree: {root}")
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("editable mode requires git to be installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"editable mode git preflight timed out for {root}") from exc
    except subprocess.SubprocessError as exc:
        raise RuntimeError(f"editable mode git preflight failed for {root}: {exc}") from exc
    if result.returncode != 0 or result.stdout.strip() != "true":
        details = (result.stderr or result.stdout or "").strip()
        suffix = f": {details}" if details else ""
        raise RuntimeError(f"editable mode requires a git worktree at {root}{suffix}")


def git_diff_hash(project_root: str, path: str) -> str:
    result = subprocess.run(
        ["git", "diff", "--", path],
        cwd=Path(project_root),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.stdout:
        payload = result.stdout
    else:
        full_path = Path(project_root) / path
        if full_path.is_file():
            payload = full_path.read_text(errors="replace")
        elif full_path.is_dir():
            parts = []
            for child in sorted(item for item in full_path.rglob("*") if item.is_file()):
                rel = child.relative_to(full_path).as_posix()
                parts.append(f"{rel}:{hashlib.sha256(child.read_bytes()).hexdigest()}")
            payload = "\n".join(parts)
        else:
            payload = ""
    return hashlib.sha256(payload.encode()).hexdigest()


def run_fixed_git_command(project_root: str, command: str) -> tuple[str, str]:
    argv = fixed_git_argv(command)
    if argv is None:
        raise ValueError("unsupported fixed verification command")
    if not is_git_repo(project_root):
        return "FAILED", f"not a git repository: {Path(project_root)}"
    result = subprocess.run(
        argv,
        cwd=Path(project_root),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    status = "OK" if result.returncode == 0 else "FAILED"
    return status, result.stdout + result.stderr


EvidenceItem = dict[str, str]
DEEP_CONTEXT_CHAR_LIMIT = 120_000
DEEP_CONTEXT_FILE_LIMIT = 24
DEEP_CONTEXT_PER_FILE_LIMIT = 20_000
RELEVANT_NAME_RE = re.compile(r"(p11b|revolut|readonly|read-only|collector|guard|control-template|key-governance)", re.I)
CONTEXT_PATH_RE = re.compile(
    r"(?<![\w/.-])((?:\.?[\w.-]+/)+[\w.@:+-]+\.(?:json|toml|yaml|yml|mjs|cjs|js|ts|md|txt))(?!\w)"
)
TEXT_SUFFIXES = {
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".json",
    ".md",
    ".toml",
    ".yaml",
    ".yml",
    ".txt",
}


def build_deep_context_evidence(project_root: str, objective: str, session_context: str | None = None) -> list[EvidenceItem]:
    """Build a bounded repo-context packet for real agent proposal quality.

    This is deliberately not generic shell access: it uses fixed git argv,
    repo-local file reads, denylisted private paths, and hard caps. The goal is
    to keep Codex's first proposal deep without letting it wander indefinitely.
    """

    root = Path(project_root)
    items: list[EvidenceItem] = []
    files: list[str] = []
    session_files = extract_context_file_paths(session_context or "")
    commit_refs = extract_commit_refs(objective)[:2]
    if commit_refs:
        items.append(
            {
                "kind": "context_scope_note",
                "status": "OK",
                "command": "consensusd targeted commit scope",
                "output": (
                    "This review is anchored to the explicit objective commit(s): "
                    f"{', '.join(ref.lower() for ref in commit_refs)}.\n"
                    "Current HEAD commit metadata is intentionally excluded from the proposal context "
                    "because it may belong to a different interactive session. Use working-tree status "
                    "only as ambient repo hygiene, not as the review target."
                ),
            }
        )
    for ref in commit_refs:
        normalized = ref.lower()
        changed = git_commit_changed_files(project_root, normalized)
        files.extend(changed)
        if changed:
            diff = git_commit_diff(project_root, normalized, changed[:DEEP_CONTEXT_FILE_LIMIT])
            items.append(
                {
                    "kind": "objective_commit_diff",
                    "status": "OK",
                    "command": f"git show --unified=120 --find-renames {normalized} -- <changed files>",
                    "output": cap_text(diff, DEEP_CONTEXT_CHAR_LIMIT),
                }
            )
    if session_files:
        files.extend(session_files)
    else:
        files.extend(discover_relevant_context_files(root))
    files = unique_preserve_order(file for file in files if safe_context_file(root, file))[:DEEP_CONTEXT_FILE_LIMIT]
    if files:
        items.append(
            {
                "kind": "deep_context_file_inventory",
                "status": "OK",
                "command": "session_context path extraction" if session_files else "repo-local bounded context discovery",
                "output": "\n".join(files),
            }
        )
        items.append(
            {
                "kind": "deep_context_file_contents",
                "status": "OK",
                "command": "repo-local bounded file reads",
                "output": cap_text(read_context_files(root, files), DEEP_CONTEXT_CHAR_LIMIT),
            }
        )
    package_json = root / "package.json"
    if package_json.is_file():
        package_context = package_scripts_context(package_json, objective)
        items.append(
            {
                "kind": "package_scripts_context",
                "status": "OK" if package_context else "SKIPPED",
                "command": "read relevant package.json scripts",
                "output": package_context or "(no relevant package scripts found)",
            }
        )
    return items


def extract_context_file_paths(text: str) -> list[str]:
    paths: list[str] = []
    for match in CONTEXT_PATH_RE.finditer(text):
        path = match.group(1).strip().strip("`'\"()[]{}<>").rstrip(".,;:")
        path = path.replace("\\", "/")
        if path.startswith("./"):
            path = path[2:]
        if path:
            paths.append(path)
    return unique_preserve_order(paths)


def git_commit_changed_files(project_root: str, ref: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", ref],
        cwd=Path(project_root),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def git_commit_diff(project_root: str, ref: str, files: list[str]) -> str:
    argv = ["git", "show", "--unified=120", "--find-renames", "--format=fuller", ref, "--", *files]
    result = subprocess.run(
        argv,
        cwd=Path(project_root),
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.stdout + result.stderr


def discover_relevant_context_files(root: Path) -> list[str]:
    candidates: list[str] = []
    for path in root.rglob("*"):
        if len(candidates) >= 80:
            break
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if not safe_context_file(root, rel):
            continue
        if RELEVANT_NAME_RE.search(rel):
            candidates.append(rel)
    return sorted(candidates, key=context_file_rank)


def context_file_rank(path: str) -> tuple[int, int, str]:
    priority = 0
    if path.startswith(("src/", "scripts/", "tests/")):
        priority -= 20
    if path.startswith((".omx/plans/", ".omx/security/", ".omx/research/", "omx_wiki/")):
        priority -= 10
    if "p11b" in path.lower():
        priority -= 10
    return priority, len(path), path


def safe_context_file(root: Path, rel: str) -> bool:
    rel = rel.replace("\\", "/").lstrip("/")
    if not rel or rel.endswith("/"):
        return False
    denied_prefixes = (
        ".git/",
        ".consensusd/",
        ".env",
        ".ssh/",
        "node_modules/",
        "dist/",
        "build/",
        ".venv/",
        "venv/",
    )
    if any(rel == prefix.rstrip("/") or rel.startswith(prefix) for prefix in denied_prefixes):
        return False
    path = root / rel
    if not path.is_file():
        return False
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return False
    try:
        return path.stat().st_size <= 250_000
    except OSError:
        return False


def read_context_files(root: Path, files: list[str]) -> str:
    blocks: list[str] = []
    for rel in files:
        path = root / rel
        try:
            text = path.read_text(errors="replace")
        except OSError as exc:
            text = f"(failed to read: {exc})"
        blocks.extend(
            [
                f"## {rel}",
                "",
                "```text",
                cap_text(text, DEEP_CONTEXT_PER_FILE_LIMIT),
                "```",
                "",
            ]
        )
    return "\n".join(blocks)


def cap_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n\n... truncated at {limit} characters by consensusd context cap ..."


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def package_scripts_context(package_json: Path, objective: str = "") -> str:
    try:
        data = json.loads(package_json.read_text(errors="replace"))
    except (OSError, json.JSONDecodeError):
        return ""
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return ""
    objective_lc = objective.lower()
    if "p11b" in objective_lc:
        domain_name = re.compile(r"(p11b|guard|collector)", re.I)
    else:
        domain_name = re.compile(r"(revolut|readonly|read-only|guard|collector)", re.I)
    generic_names = {"test", "check", "check:syntax", "lint", "typecheck", "type:check"}
    selected: dict[str, str] = {}
    for key, value in scripts.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if key in generic_names or domain_name.search(f"{key} {value}"):
            selected[key] = value
    if not selected:
        return ""
    return json.dumps({"scripts": selected}, indent=2, sort_keys=True)


def unique_preserve_order(values) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


COMMIT_REF_RE = re.compile(r"\b[0-9a-fA-F]{7,40}\b")


def initial_git_evidence_commands(objective: str) -> list[tuple[str, str, str]]:
    """Return fixed, safe git evidence commands for a new review run.

    The working tree diff alone is misleading after the user has committed the
    change they want reviewed. If the objective names commit refs, we anchor
    evidence to those refs and avoid HEAD commit metadata because HEAD may be
    an unrelated cleanup from another interactive session.
    """
    objective_refs = extract_commit_refs(objective)
    commands: list[tuple[str, str, str]] = [
        ("git_status_short", "git status --short", "(working tree clean)"),
        ("git_diff_stat", "git diff --stat", "(no working-tree diff reported by git diff --stat)"),
    ]
    if not objective_refs:
        commands.extend(
            [
                ("git_head_summary", "git show --stat --oneline --decorate HEAD", "(no HEAD commit summary available)"),
                (
                    "git_head_name_status",
                    "git show --name-status --oneline --decorate HEAD",
                    "(no HEAD name-status available)",
                ),
            ]
        )
    seen: set[str] = {"HEAD"}
    for ref in objective_refs:
        normalized = ref.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        commands.append(
            (
                "git_objective_commit_summary",
                f"git show --stat --oneline --decorate {normalized}",
                f"(no summary available for objective commit {normalized})",
            )
        )
        commands.append(
            (
                "git_objective_commit_name_status",
                f"git show --name-status --oneline --decorate {normalized}",
                f"(no name-status available for objective commit {normalized})",
            )
        )
    return commands


def extract_commit_refs(text: str) -> list[str]:
    return [match.group(0) for match in COMMIT_REF_RE.finditer(text)]


def fixed_git_argv(command: str) -> Optional[list[str]]:
    allowed = {
        "git diff --stat": ["git", "diff", "--stat"],
        "git diff": ["git", "diff"],
        "git status --short": ["git", "status", "--short"],
        "git show --stat --oneline --decorate HEAD": ["git", "show", "--stat", "--oneline", "--decorate", "HEAD"],
        "git show --name-status --oneline --decorate HEAD": [
            "git",
            "show",
            "--name-status",
            "--oneline",
            "--decorate",
            "HEAD",
        ],
    }
    if command in allowed:
        return allowed[command]
    stat_prefix = "git show --stat --oneline --decorate "
    name_prefix = "git show --name-status --oneline --decorate "
    if command.startswith(stat_prefix):
        ref = command.removeprefix(stat_prefix)
        if COMMIT_REF_RE.fullmatch(ref):
            return ["git", "show", "--stat", "--oneline", "--decorate", ref]
    if command.startswith(name_prefix):
        ref = command.removeprefix(name_prefix)
        if COMMIT_REF_RE.fullmatch(ref):
            return ["git", "show", "--name-status", "--oneline", "--decorate", ref]
    return None


def changed_files_from_diff_stat(stat_output: str) -> list[str]:
    files: list[str] = []
    for line in stat_output.splitlines():
        if "|" not in line:
            continue
        files.append(line.split("|", 1)[0].strip())
    return files

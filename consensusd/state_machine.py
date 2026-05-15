from __future__ import annotations

from .models import RunStatus, TERMINAL_STATUSES


ALLOWED_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.INIT: {RunStatus.AWAITING_CODEX_PROPOSAL},
    RunStatus.AWAITING_CODEX_PROPOSAL: {RunStatus.CODEX_DRAFTING},
    RunStatus.CODEX_DRAFTING: {RunStatus.AWAITING_KIMI_REVIEW},
    RunStatus.AWAITING_KIMI_REVIEW: {RunStatus.KIMI_REVIEWING},
    RunStatus.KIMI_REVIEWING: {RunStatus.REVISION_REQUESTED, RunStatus.CONSENSUS_LOCKED},
    RunStatus.REVISION_REQUESTED: {RunStatus.AWAITING_CODEX_PROPOSAL},
    RunStatus.CONSENSUS_LOCKED: {RunStatus.AWAITING_OMX},
    RunStatus.AWAITING_OMX: {RunStatus.OMX_GENERATING},
    RunStatus.OMX_GENERATING: {RunStatus.OMX_GENERATED},
    RunStatus.OMX_GENERATED: {RunStatus.AWAITING_HUMAN_APPROVAL},
    RunStatus.AWAITING_HUMAN_APPROVAL: {RunStatus.RALPH_HANDOFF_APPROVED},
    RunStatus.RALPH_HANDOFF_APPROVED: {RunStatus.RALPH_HANDOFF_COMPLETE},
    RunStatus.RALPH_HANDOFF_COMPLETE: set(),
    RunStatus.FAILED: set(),
    RunStatus.CANCELLED: set(),
}


class InvalidTransition(ValueError):
    pass


def is_terminal(status: RunStatus) -> bool:
    return status in TERMINAL_STATUSES


def assert_transition(current: RunStatus, new: RunStatus) -> None:
    if current in TERMINAL_STATUSES:
        raise InvalidTransition(f"cannot transition terminal run from {current} to {new}")
    if new in {RunStatus.FAILED, RunStatus.CANCELLED}:
        return
    if new not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransition(f"invalid transition {current} -> {new}")

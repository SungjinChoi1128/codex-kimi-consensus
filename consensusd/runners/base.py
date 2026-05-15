from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Callable
from typing import Protocol
from typing import Optional

from consensusd.models import Evidence, Proposal, Review, ReviewDecision, Run

CancelCheck = Callable[[], bool]

_cancel_check: ContextVar[Optional[CancelCheck]] = ContextVar("consensusd_cancel_check", default=None)


class RunnerCancelled(RuntimeError):
    """Raised when a durable run cancellation interrupts an agent runner."""


def current_cancel_check() -> Optional[CancelCheck]:
    return _cancel_check.get()


def set_cancel_check(check: Optional[CancelCheck]) -> Token[Optional[CancelCheck]]:
    return _cancel_check.set(check)


def reset_cancel_check(token: Token[Optional[CancelCheck]]) -> None:
    _cancel_check.reset(token)


class AgentRunner(Protocol):
    def generate_proposal(self, run: Run, prior_review: Optional[Review], evidence: list[Evidence]) -> str: ...

    def review_proposal(self, run: Run, proposal: Proposal, evidence: list[Evidence]) -> ReviewDecision: ...

    def generate_omx(self, run: Run, consensus_proposal: Proposal, evidence: list[Evidence]) -> str: ...


class RevisionRunner(Protocol):
    def apply_revision(self, run: Run, prior_review: Review, evidence: list[Evidence]) -> str: ...

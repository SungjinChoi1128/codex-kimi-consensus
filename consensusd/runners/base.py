from __future__ import annotations

from typing import Protocol
from typing import Optional

from consensusd.models import Evidence, Proposal, Review, ReviewDecision, Run


class AgentRunner(Protocol):
    def generate_proposal(self, run: Run, prior_review: Optional[Review], evidence: list[Evidence]) -> str: ...

    def review_proposal(self, run: Run, proposal: Proposal, evidence: list[Evidence]) -> ReviewDecision: ...

    def generate_omx(self, run: Run, consensus_proposal: Proposal, evidence: list[Evidence]) -> str: ...


class RevisionRunner(Protocol):
    def apply_revision(self, run: Run, prior_review: Review, evidence: list[Evidence]) -> str: ...

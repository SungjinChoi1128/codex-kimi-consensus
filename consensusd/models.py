from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class RunStatus(str, Enum):
    INIT = "INIT"
    AWAITING_CODEX_PROPOSAL = "AWAITING_CODEX_PROPOSAL"
    CODEX_DRAFTING = "CODEX_DRAFTING"
    AWAITING_KIMI_REVIEW = "AWAITING_KIMI_REVIEW"
    KIMI_REVIEWING = "KIMI_REVIEWING"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    CONSENSUS_LOCKED = "CONSENSUS_LOCKED"
    AWAITING_OMX = "AWAITING_OMX"
    OMX_GENERATING = "OMX_GENERATING"
    OMX_GENERATED = "OMX_GENERATED"
    AWAITING_HUMAN_APPROVAL = "AWAITING_HUMAN_APPROVAL"
    RALPH_HANDOFF_APPROVED = "RALPH_HANDOFF_APPROVED"
    RALPH_HANDOFF_COMPLETE = "RALPH_HANDOFF_COMPLETE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATUSES = {
    RunStatus.RALPH_HANDOFF_COMPLETE,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
}


class ReviewStatus(str, Enum):
    APPROVED = "APPROVED"
    NEEDS_REVISION = "NEEDS_REVISION"


class ToolRole(str, Enum):
    CONTROL_SURFACE = "control_surface"
    CODEX_PLANNER = "codex_planner"
    KIMI_REVIEWER = "kimi_reviewer"
    ORCHESTRATOR = "orchestrator"


class Run(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: str
    project_root: str
    objective: str
    status: RunStatus
    mode: str
    runner_mode: str = "mock"
    max_rounds: int = 5
    current_round: int = 1
    version: int = 0
    created_at: str
    updated_at: str
    locked_at: Optional[str] = None
    error: Optional[str] = None


class Proposal(BaseModel):
    proposal_id: str
    run_id: str
    round: int
    runner: str = "unknown"
    content: str
    created_at: str


class Review(BaseModel):
    review_id: str
    run_id: str
    round: int
    runner: str = "unknown"
    status: ReviewStatus
    content: str
    created_at: str


class Evidence(BaseModel):
    evidence_id: str
    run_id: str
    round: int
    kind: str
    command: Optional[str] = None
    status: str
    output: str
    created_at: str


class OmxPlan(BaseModel):
    omx_id: str
    run_id: str
    runner: str = "unknown"
    path: Optional[str] = None
    content: str
    created_at: str


class ContextBridge(BaseModel):
    bridge_id: str
    run_id: str
    path: str
    content: str
    created_at: str


class Event(BaseModel):
    event_id: str
    run_id: str
    sequence: int
    event_type: str
    payload_json: str
    created_at: str


class RalphHandoff(BaseModel):
    handoff_id: str
    run_id: str
    packet_json: str
    status: str
    created_at: str


class ReviewDecision(BaseModel):
    status: ReviewStatus
    content: str


class Transcript(BaseModel):
    run: Run
    proposals: list[Proposal] = Field(default_factory=list)
    reviews: list[Review] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    omx_plans: list[OmxPlan] = Field(default_factory=list)
    context_bridges: list[ContextBridge] = Field(default_factory=list)
    ralph_handoffs: list[RalphHandoff] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)


class ToolRequest(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResponse(BaseModel):
    ok: bool
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None


ReviewLiteral = Literal["APPROVED", "NEEDS_REVISION"]

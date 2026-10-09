"""Investigation lifecycle and persisted investigation model (agent-safe data only)."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from app.agent.models import AgentEvent
from app.agent.runner import InvestigationRecord
from app.data.models import PublicCase
from app.evaluation.models import EvaluationResult


class InvestigationStatus(StrEnum):
    CREATED = "CREATED"
    PREPARING = "PREPARING"
    INVESTIGATING = "INVESTIGATING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    PATCHING = "PATCHING"
    EVALUATING = "EVALUATING"
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


S = InvestigationStatus

# Legal lifecycle edges. Terminal statuses have no outgoing edges.
TRANSITIONS: dict[InvestigationStatus, frozenset[InvestigationStatus]] = {
    S.CREATED: frozenset({S.PREPARING, S.FAILED, S.CANCELLED}),
    S.PREPARING: frozenset({S.INVESTIGATING, S.FAILED, S.CANCELLED}),
    S.INVESTIGATING: frozenset({S.AWAITING_APPROVAL, S.UNRESOLVED, S.FAILED, S.CANCELLED}),
    S.AWAITING_APPROVAL: frozenset({S.PATCHING, S.UNRESOLVED, S.CANCELLED}),
    # PATCHING -> EVALUATING once the approved patch is applied; -> UNRESOLVED when
    # the apply was refused or failed (nothing to evaluate).
    S.PATCHING: frozenset({S.EVALUATING, S.UNRESOLVED, S.FAILED, S.CANCELLED}),
    S.EVALUATING: frozenset({S.RESOLVED, S.UNRESOLVED, S.AWAITING_APPROVAL, S.FAILED, S.CANCELLED}),
    S.RESOLVED: frozenset(),
    S.UNRESOLVED: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
}

TERMINAL_STATUSES = frozenset(s for s, targets in TRANSITIONS.items() if not targets)
# Statuses driven by an in-process background task; stale after a restart.
RUNNING_STATUSES = frozenset({S.CREATED, S.PREPARING, S.INVESTIGATING, S.PATCHING, S.EVALUATING})


class InvalidTransitionError(ValueError):
    def __init__(self, current: InvestigationStatus, target: InvestigationStatus) -> None:
        super().__init__(f"illegal status transition {current} -> {target}")
        self.current = current
        self.target = target


def allowed_transitions(status: InvestigationStatus) -> list[InvestigationStatus]:
    return sorted(TRANSITIONS[status])


def check_transition(current: InvestigationStatus, target: InvestigationStatus) -> None:
    if target not in TRANSITIONS[current]:
        raise InvalidTransitionError(current, target)


class DecisionConflictError(ValueError):
    """A decision that the current state does not accept (wrong status, no patch, decided)."""


class ApprovalDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class Approval(BaseModel):
    """A human decision on one exact proposed patch (bound by its sha256)."""

    decision: ApprovalDecision
    patch_id: str
    patch_sha256: str
    decided_at: datetime


class StoredInvestigation(BaseModel):
    """One investigation. `record` reuses the agent's InvestigationRecord snapshot;
    `events` is the full timeline (agent + lifecycle events)."""

    investigation_id: str
    status: InvestigationStatus
    case: PublicCase
    created_at: datetime
    updated_at: datetime
    error: str | None = None
    events: list[AgentEvent] = Field(default_factory=list)
    record: InvestigationRecord | None = None
    approval: Approval | None = None
    evaluation: EvaluationResult | None = None  # gold-free benchmark verdict and metrics


class InvestigationSummary(BaseModel):
    investigation_id: str
    instance_id: str
    status: InvestigationStatus
    created_at: datetime
    updated_at: datetime

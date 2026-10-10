"""Investigation create/read/cancel/decision endpoints. Responses carry agent-safe data only."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.agent.models import (
    AgentEvent,
    Evidence,
    FinalReport,
    Hypothesis,
    InvestigationPlan,
    IssueAnalysis,
    PatchResult,
    ProposedPatch,
    Suspect,
)
from app.data.models import PublicCase
from app.evaluation.models import EvaluationResult
from app.services.investigations import (
    CaseNotFoundError,
    InvestigationService,
    StepResult,
    StepwiseDisabledError,
)
from app.storage.models import (
    Approval,
    ApprovalDecision,
    DecisionConflictError,
    InvalidTransitionError,
    InvestigationStatus,
    StoredInvestigation,
    allowed_transitions,
)
from app.storage.sqlite import InvestigationNotFoundError

router = APIRouter(prefix="/api/investigations")


class CreateInvestigationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.\-]+$")


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ApprovalDecision


class CreateInvestigationResponse(BaseModel):
    investigation_id: UUID
    status: InvestigationStatus


class InvestigationResponse(BaseModel):
    investigation_id: UUID
    status: InvestigationStatus
    allowed_transitions: list[InvestigationStatus]
    case: PublicCase
    created_at: datetime
    updated_at: datetime
    error: str | None
    analysis: IssueAnalysis | None = None
    plan: InvestigationPlan | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    suspects: list[Suspect] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    current_hypothesis: Hypothesis | None = None
    report: FinalReport | None = None  # root cause, grounded claims, confidence
    agent_errors: list[str] = Field(default_factory=list)
    limits_reached: list[str] = Field(default_factory=list)
    tool_call_count: int = 0
    iteration: int = 0
    proposed_patch: ProposedPatch | None = None
    patch_error: str | None = None
    approval: Approval | None = None
    patch_result: PatchResult | None = None
    evaluation: EvaluationResult | None = None  # gold-free SWE-bench verdict and metrics
    events: list[AgentEvent] = Field(default_factory=list)

    @classmethod
    def from_stored(cls, stored: StoredInvestigation) -> "InvestigationResponse":
        response = cls(
            investigation_id=UUID(stored.investigation_id),
            status=stored.status,
            allowed_transitions=allowed_transitions(stored.status),
            case=stored.case,
            created_at=stored.created_at,
            updated_at=stored.updated_at,
            error=stored.error,
            approval=stored.approval,
            evaluation=stored.evaluation,
            events=stored.events,
        )
        record = stored.record
        if record is None:
            return response
        return response.model_copy(
            update={
                "analysis": record.analysis,
                "plan": record.plan,
                "evidence": record.evidence,
                "suspects": record.suspects,
                "hypotheses": record.hypotheses,
                "current_hypothesis": record.hypotheses[-1] if record.hypotheses else None,
                "report": record.final_report,
                "agent_errors": record.errors,
                "limits_reached": record.limits_reached,
                "tool_call_count": record.tool_call_count,
                "iteration": record.iteration,
                "proposed_patch": record.proposed_patch,
                "patch_error": record.patch_error,
                "patch_result": record.patch_result,
            }
        )


class AdvanceResponse(BaseModel):
    """Outcome of one bounded stepwise step (see InvestigationService.advance)."""

    result: StepResult
    pending: bool  # call advance again: the run has more work it can do now
    investigation: InvestigationResponse


def get_service(request: Request) -> InvestigationService:
    return request.app.state.investigations


Service = Annotated[InvestigationService, Depends(get_service)]


def _not_found(investigation_id: UUID) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"investigation {investigation_id} not found")


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_investigation(
    body: CreateInvestigationRequest, service: Service
) -> CreateInvestigationResponse:
    try:
        stored = service.start(body.instance_id)
    except CaseNotFoundError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"unknown case {body.instance_id!r}"
        ) from exc
    return CreateInvestigationResponse(
        investigation_id=UUID(stored.investigation_id), status=stored.status
    )


@router.get("/{investigation_id}")
def get_investigation(investigation_id: UUID, service: Service) -> InvestigationResponse:
    try:
        stored = service.get(str(investigation_id))
    except InvestigationNotFoundError as exc:
        raise _not_found(investigation_id) from exc
    return InvestigationResponse.from_stored(stored)


@router.post("/{investigation_id}/cancel")
async def cancel_investigation(investigation_id: UUID, service: Service) -> InvestigationResponse:
    """Cancel a non-terminal investigation; 409 when the status does not allow it."""
    try:
        stored = service.cancel(str(investigation_id))
    except InvestigationNotFoundError as exc:
        raise _not_found(investigation_id) from exc
    except InvalidTransitionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return InvestigationResponse.from_stored(stored)


@router.post("/{investigation_id}/decision")
async def decide(
    investigation_id: UUID, body: DecisionRequest, service: Service
) -> InvestigationResponse:
    """Approve or reject the proposed patch; 409 unless the status is AWAITING_APPROVAL."""
    try:
        stored = service.decide(str(investigation_id), body.decision)
    except InvestigationNotFoundError as exc:
        raise _not_found(investigation_id) from exc
    except (DecisionConflictError, InvalidTransitionError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return InvestigationResponse.from_stored(stored)


@router.post("/{investigation_id}/advance")
async def advance(investigation_id: UUID, service: Service) -> AdvanceResponse:
    """Stepwise (serverless) mode: run the next bounded step and persist it.

    Idempotent and safe to call concurrently: "busy" while another request holds the
    step, "idle" while awaiting a decision or finished. 409 in background mode."""
    try:
        outcome = await service.advance(str(investigation_id))
    except InvestigationNotFoundError as exc:
        raise _not_found(investigation_id) from exc
    except StepwiseDisabledError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return AdvanceResponse(
        result=outcome.result,
        pending=outcome.pending,
        investigation=InvestigationResponse.from_stored(outcome.stored),
    )

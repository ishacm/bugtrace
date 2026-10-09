"""Explicit LangGraph state for one investigation."""

from typing import Literal, TypedDict

from pydantic import BaseModel

from app.agent.models import (
    AgentEvent,
    ApprovalStatus,
    Evidence,
    FinalReport,
    Hypothesis,
    InvestigationPlan,
    IssueAnalysis,
    PatchResult,
    ProposedPatch,
    RootCause,
    Sufficiency,
    Suspect,
)
from app.data.models import PublicCase

NextStep = Literal["search_repository", "produce_root_cause"]


class CandidateHit(BaseModel):
    path: str
    line: int
    snippet: str
    score: int


class InvestigationState(TypedDict, total=False):
    investigation_id: str
    case: PublicCase  # agent-safe data only; never a raw dataset row
    analysis: IssueAnalysis | None
    plan: InvestigationPlan | None

    evidence: list[Evidence]
    candidates: list[CandidateHit]
    suspects: list[Suspect]
    hypotheses: list[Hypothesis]
    current_hypothesis: Hypothesis | None
    sufficiency: Sufficiency | None
    root_cause: RootCause | None
    final_report: FinalReport | None

    pending_symbols: list[str]
    pending_queries: list[str]
    searches_performed: list[str]
    files_inspected: list[str]

    tool_call_count: int
    iteration: int
    max_iterations: int
    max_tool_calls: int
    next_step: NextStep
    limits_reached: list[str]

    errors: list[str]
    events: list[AgentEvent]

    # V2 patch stage. The graph pauses (interrupt) while approval_status is "pending".
    max_patch_attempts: int
    proposed_patch: ProposedPatch | None
    patch_error: str | None
    approval_status: ApprovalStatus | None
    patch_result: PatchResult | None
    test_result: str | None  # TASK 009

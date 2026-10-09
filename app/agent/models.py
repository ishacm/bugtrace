"""Structured agent records (evidence, suspects, hypotheses, report) and LLM output schemas."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Confidence(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class EvidenceType(StrEnum):
    ISSUE = "issue"
    CODE = "code"
    GIT = "git"
    TEST = "test"
    RUNTIME = "runtime"


class Evidence(BaseModel):
    evidence_id: str
    type: EvidenceType
    source: str  # tool name or "problem_statement"
    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    excerpt: str
    summary: str
    relevance: str = ""


class Suspect(BaseModel):
    path: str
    symbol: str | None = None
    rank: int
    score: int  # explicit: distinct supporting evidence + code-evidence bonus
    reason: str
    evidence_ids: list[str]


class Hypothesis(BaseModel):
    iteration: int
    statement: str
    evidence_ids: list[str]
    contradictions: list[str] = Field(default_factory=list)


class Claim(BaseModel):
    statement: str
    evidence_ids: list[str]


class Sufficiency(BaseModel):
    sufficient: bool
    confidence: Confidence
    reasons: list[str]


class RootCause(BaseModel):
    summary: str
    claims: list[Claim]
    open_questions: list[str] = Field(default_factory=list)
    confidence: Confidence
    dropped_claims: list[str] = Field(default_factory=list)
    generated: bool = True  # False when the model failed and a fallback was used


class FinalReport(BaseModel):
    investigation_id: str
    instance_id: str
    status: Literal["COMPLETED", "PARTIAL"]
    confidence: Confidence
    summary: str
    claims: list[Claim]
    suspects: list[Suspect]
    cited_evidence_ids: list[str]
    open_questions: list[str]
    limits_reached: list[str]
    tool_calls: int
    iterations: int


EventType = Literal[
    "case_started",
    "repo_ready",
    "plan_created",
    "search_started",
    "evidence_found",
    "suspect_found",
    "suspect_removed",
    "hypothesis_created",
    "hypothesis_revised",
    "root_cause_found",
    "case_closed",
    "error",
]


class AgentEvent(BaseModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    type: EventType
    title: str
    message: str
    metadata: dict[str, Any] = Field(default_factory=dict)


# --- LLM structured outputs ---------------------------------------------------
# Kept flat (no defaults/unions) so they convert cleanly to model response schemas.


class IssueAnalysis(BaseModel):
    summary: str
    expected_behavior: str
    actual_behavior: str
    mentioned_symbols: list[str]
    mentioned_files: list[str]
    keywords: list[str]


class InvestigationPlan(BaseModel):
    steps: list[str]
    search_queries: list[str]
    symbols: list[str]


class FileReadRequest(BaseModel):
    path: str
    start_line: int
    end_line: int
    reason: str


class CandidateSelection(BaseModel):
    reads: list[FileReadRequest]


class SuspectDraft(BaseModel):
    path: str
    symbol: str
    reason: str
    evidence_ids: list[str]


class HypothesisDraft(BaseModel):
    statement: str
    evidence_ids: list[str]
    suspects: list[SuspectDraft]
    contradictions: list[str]
    follow_up_queries: list[str]
    follow_up_symbols: list[str]


class RootCauseDraft(BaseModel):
    summary: str
    claims: list[Claim]
    open_questions: list[str]

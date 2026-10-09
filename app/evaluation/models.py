"""Evaluation request/result models. Gold-free: safe for storage, API and UI.

Results carry verdicts, counts and metrics only. They never contain the reference
(gold) patch, test patch, test names, hints or raw evaluator logs, and they never
flow back into agent state.
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EvaluationStatus(StrEnum):
    RESOLVED = "RESOLVED"  # the harness ran the tests and the issue is resolved
    UNRESOLVED = "UNRESOLVED"  # the harness ran and the patch did not resolve the issue
    ERROR = "ERROR"  # the harness could not produce a test verdict
    TIMEOUT = "TIMEOUT"
    UNAVAILABLE = "UNAVAILABLE"  # evaluation environment missing (e.g. no Docker)


FailureReason = Literal[
    "EVALUATION_DISABLED",
    "DOCKER_UNAVAILABLE",
    "HARNESS_UNAVAILABLE",
    "REFERENCE_UNAVAILABLE",
    "REFERENCE_MISMATCH",
    "WORKSPACE_UNSAFE",
    "PATCH_APPLY_FAILED",
    "TESTS_TIMED_OUT",
    "HARNESS_TIMED_OUT",
    "HARNESS_FAILED",
    "INFRA_FAILURE",
    "REPORT_MISSING",
    "INTERNAL_ERROR",
]


class EvaluationRequest(BaseModel):
    """What the service hands the evaluator after an approved patch was applied.

    Built from persisted agent output only; the evaluator adds reference data itself."""

    model_config = ConfigDict(frozen=True)

    investigation_id: str
    instance_id: str
    base_commit: str
    patch_id: str
    patch_sha256: str
    candidate_diff: str  # the approved, applied diff (sha256-bound)
    suspect_files: list[str]  # agent ranking, best first, unique
    changed_files: list[str]  # workspace changes observed after apply


class PassFailCounts(BaseModel):
    """Pass/fail counts for one test group. Never test names."""

    passed: int = 0
    failed: int = 0


class LocalizationMetrics(BaseModel):
    """Agent suspects vs files modified by the reference fix (evaluator-computed)."""

    reference_file_count: int
    suspect_file_count: int
    top1_file_hit: bool
    top3_file_recall: float  # share of reference files among the top-3 suspect files
    candidate_file_overlap: int  # candidate-patch files that the reference fix also edits


class TraceMetrics(BaseModel):
    """Efficiency and grounding read from the persisted execution trace."""

    tool_calls: int
    iterations: int
    evidence_items: int
    claims: int
    grounded_claims: int  # claims citing at least one recorded evidence id
    agent_seconds: float | None = None  # case opened -> patch awaiting approval
    model_calls: int | None = None  # not recorded by the agent yet
    token_usage: int | None = None  # not recorded by the agent yet


class SafetyMetrics(BaseModel):
    approved_files: list[str]
    changed_files: list[str]
    unauthorized_writes: int  # changed files outside the approved patch


class HarnessInfo(BaseModel):
    """Everything needed to reproduce the run."""

    name: str = "swebench"
    version: str
    dataset: str
    split: str
    run_id: str
    model_name: str
    test_timeout_seconds: int
    reference_row_sha256: str | None = None
    prediction_sha256: str | None = None
    exit_code: int | None = None


class EvaluationResult(BaseModel):
    status: EvaluationStatus
    resolved: bool | None = None  # set only when the harness produced a test verdict
    message: str
    failure_reason: FailureReason | None = None
    patch_id: str
    patch_sha256: str
    patch_applied_in_evaluator: bool | None = None
    target_tests: PassFailCounts | None = None  # tests the fix must make pass
    regression_tests: PassFailCounts | None = None  # tests that must keep passing
    localization: LocalizationMetrics | None = None
    trace: TraceMetrics | None = None
    safety: SafetyMetrics | None = None
    harness: HarnessInfo | None = None
    started_at: datetime
    finished_at: datetime
    duration_seconds: float = Field(ge=0)

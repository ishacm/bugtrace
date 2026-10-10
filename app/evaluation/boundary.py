"""The gold-free side of the evaluation boundary.

The service and the web app import only this module. It never imports reference
(gold) data, the harness or the raw dataset adapter: `build_evaluator` loads the
SWE-bench evaluator lazily, and only when evaluation is enabled. Deployments with
evaluation disabled (Cloud Run, Vercel) therefore never load gold-data modules.
"""

from datetime import UTC, datetime
from typing import Protocol

from app.agent.patches import patch_sha256
from app.config import Settings
from app.evaluation.metrics import suspect_files
from app.evaluation.models import (
    EvaluationRequest,
    EvaluationResult,
    EvaluationStatus,
    FailureReason,
)
from app.storage.models import ApprovalDecision, InvestigationStatus, StoredInvestigation


class Evaluator(Protocol):
    async def evaluate(self, request: EvaluationRequest) -> EvaluationResult: ...


class EvaluationRefusedError(RuntimeError):
    """The investigation is not in a state that may be evaluated."""


def build_request(stored: StoredInvestigation, changed_files: list[str]) -> EvaluationRequest:
    """Authorize evaluation and build the request from persisted agent output.

    Requires status EVALUATING, an `approve` decision bound to the stored patch's
    sha256, and a recorded successful apply."""
    if stored.status != InvestigationStatus.EVALUATING:
        raise EvaluationRefusedError(f"investigation is {stored.status}, not EVALUATING")
    approval = stored.approval
    if approval is None or approval.decision != ApprovalDecision.APPROVE:
        raise EvaluationRefusedError("no approval recorded")
    record = stored.record
    patch = record.proposed_patch if record else None
    if record is None or patch is None:
        raise EvaluationRefusedError("no proposed patch")
    if not (patch_sha256(patch.diff) == patch.sha256 == approval.patch_sha256):
        raise EvaluationRefusedError("patch does not match the approved patch")
    if record.patch_result is None or not record.patch_result.applied:
        raise EvaluationRefusedError("the approved patch was not applied")
    return EvaluationRequest(
        investigation_id=stored.investigation_id,
        instance_id=stored.case.instance_id,
        base_commit=stored.case.base_commit,
        patch_id=patch.patch_id,
        patch_sha256=patch.sha256,
        candidate_diff=patch.diff,
        suspect_files=suspect_files(stored),
        changed_files=changed_files,
    )


class DisabledEvaluator:
    """Evaluator for deployments without Docker (Cloud Run, the Vercel demo).

    It never loads reference data and never starts the harness; the case ends
    UNRESOLVED with no test verdict. Grade such patches locally or in GitHub Actions."""

    async def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        return failed_result(
            request,
            EvaluationStatus.UNAVAILABLE,
            "EVALUATION_DISABLED",
            "Full SWE-bench evaluation requires the local/manual evaluation environment.",
            datetime.now(UTC),
        )


def build_evaluator(settings: Settings) -> Evaluator:
    if not settings.evaluation_enabled:
        return DisabledEvaluator()
    from app.evaluation.evaluator import SwebenchEvaluator  # loads gold data access

    return SwebenchEvaluator.from_settings(settings)


def failed_result(
    request: EvaluationRequest,
    status: EvaluationStatus,
    reason: FailureReason,
    message: str,
    started: datetime,
) -> EvaluationResult:
    """A result for an evaluation that crashed or was cut off outside the evaluator."""
    finished = datetime.now(UTC)
    return EvaluationResult(
        status=status,
        message=message,
        failure_reason=reason,
        patch_id=request.patch_id,
        patch_sha256=request.patch_sha256,
        started_at=started,
        finished_at=finished,
        duration_seconds=round((finished - started).total_seconds(), 3),
    )

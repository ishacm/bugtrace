"""SWE-bench evaluation of an approved, applied BugTrace patch.

This is the evaluation boundary. Reference (gold) data is loaded here, by
`instance_id`, only after the agent has finished and a human-approved patch was
applied. The candidate is compared with the reference only through the official
harness; the verdict comes from its test results, never from a model. What leaves
this module is a gold-free `EvaluationResult`.
"""

import asyncio
import json
import re
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from app.agent.patches import patch_sha256
from app.config import Settings
from app.evaluation.gold import (
    GoldSource,
    ReferenceUnavailableError,
    gold_case_from_row,
    modified_files,
    row_sha256,
)
from app.evaluation.harness import (
    MODEL_NAME,
    HarnessOutcome,
    Prediction,
    default_command_prefix,
    harness_args,
    parse_outcome,
    remove_run_containers,
    run_harness,
    write_predictions,
)
from app.evaluation.metrics import localization_metrics, safety_metrics, suspect_files
from app.evaluation.models import (
    EvaluationRequest,
    EvaluationResult,
    EvaluationStatus,
    FailureReason,
    HarnessInfo,
    LocalizationMetrics,
)
from app.storage.models import ApprovalDecision, InvestigationStatus, StoredInvestigation
from app.workspace.runner import CommandError, run_command

_RUN_ID_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


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


def docker_preflight() -> str | None:
    """None when a Docker daemon answers; otherwise why evaluation cannot run."""
    if shutil.which("docker") is None:
        return "Docker is not installed; the SWE-bench harness needs a Docker daemon."
    try:
        run_command(
            ["docker", "info", "--format", "{{.ServerVersion}}"], cwd=Path.cwd(), timeout=30
        )
    except CommandError:
        return "The Docker daemon is not reachable; start Docker to run SWE-bench evaluation."
    return None


class SwebenchEvaluator:
    def __init__(
        self,
        *,
        gold: GoldSource,
        runs_dir: Path,
        harness_version: str,
        python: str,
        timeout_seconds: float,
        test_timeout_seconds: int,
        command_prefix: list[str] | None = None,
        preflight: Callable[[], str | None] = docker_preflight,
    ) -> None:
        self.gold = gold
        self.runs_dir = runs_dir
        self.harness_version = harness_version
        self.python = python
        self.timeout_seconds = timeout_seconds
        self.test_timeout_seconds = test_timeout_seconds
        self.command_prefix = command_prefix
        self.preflight = preflight

    @classmethod
    def from_settings(cls, settings: Settings) -> "SwebenchEvaluator":
        root = settings.evaluation_dir.resolve()
        return cls(
            gold=GoldSource(
                root / "reference", settings.evaluation_dataset, settings.evaluation_split
            ),
            runs_dir=root / "runs",
            harness_version=settings.evaluation_harness_version,
            python=settings.evaluation_python,
            timeout_seconds=settings.evaluation_timeout_seconds,
            test_timeout_seconds=settings.evaluation_test_timeout_seconds,
        )

    async def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        started = datetime.now(UTC)
        candidate_files = modified_files(request.candidate_diff)
        safety = safety_metrics(request, candidate_files)
        localization: LocalizationMetrics | None = None
        harness: HarnessInfo | None = None

        def finish(
            status: EvaluationStatus,
            message: str,
            reason: FailureReason | None = None,
            outcome: HarnessOutcome | None = None,
        ) -> EvaluationResult:
            finished = datetime.now(UTC)
            extra: dict[str, Any] = {}
            if outcome is not None:
                extra = {
                    "resolved": outcome.resolved,
                    "patch_applied_in_evaluator": outcome.patch_applied,
                    "target_tests": outcome.target_tests,
                    "regression_tests": outcome.regression_tests,
                }
            return EvaluationResult(
                status=status,
                message=message,
                failure_reason=reason,
                patch_id=request.patch_id,
                patch_sha256=request.patch_sha256,
                localization=localization,
                safety=safety,
                harness=harness,
                started_at=started,
                finished_at=finished,
                duration_seconds=round((finished - started).total_seconds(), 3),
                **extra,
            )

        if safety.unauthorized_writes:
            return finish(
                EvaluationStatus.ERROR,
                "The workspace has changes outside the approved patch; evaluation refused.",
                "WORKSPACE_UNSAFE",
            )

        # Reference data enters only here, after the agent has finished.
        try:
            row = await asyncio.to_thread(self.gold.row, request.instance_id)
            reference = gold_case_from_row(row)
        except (ReferenceUnavailableError, KeyError, ValueError) as exc:
            return finish(
                EvaluationStatus.ERROR,
                f"Benchmark reference data is unavailable ({type(exc).__name__}).",
                "REFERENCE_UNAVAILABLE",
            )
        if row.get("base_commit") != request.base_commit:
            return finish(
                EvaluationStatus.ERROR,
                "The evaluator's reference row has a different base commit than the case.",
                "REFERENCE_MISMATCH",
            )
        localization = localization_metrics(
            modified_files(reference.patch), request.suspect_files, candidate_files
        )

        run_id = _RUN_ID_UNSAFE.sub(
            "-", f"bugtrace-{request.investigation_id[:8]}-{started:%Y%m%dT%H%M%S}"
        )
        harness = HarnessInfo(
            version=self.harness_version,
            dataset=self.gold.dataset,
            split=self.gold.split,
            run_id=run_id,
            model_name=MODEL_NAME,
            test_timeout_seconds=self.test_timeout_seconds,
            reference_row_sha256=row_sha256(row),
        )

        unavailable = await asyncio.to_thread(self.preflight)
        if unavailable:
            return finish(EvaluationStatus.UNAVAILABLE, unavailable, "DOCKER_UNAVAILABLE")
        prefix = self.command_prefix or default_command_prefix(self.harness_version, self.python)
        if prefix is None:
            return finish(
                EvaluationStatus.UNAVAILABLE,
                "uv is not on PATH; it is needed to run the pinned SWE-bench harness.",
                "HARNESS_UNAVAILABLE",
            )

        run_dir = self.runs_dir / request.investigation_id / run_id
        report_dir = run_dir / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        dataset_path = run_dir / "dataset.json"
        dataset_path.write_text(json.dumps([row]), encoding="utf-8")
        predictions_path = run_dir / "predictions.jsonl"
        prediction_sha = write_predictions(
            predictions_path,
            [Prediction(instance_id=request.instance_id, model_patch=request.candidate_diff)],
        )
        harness = harness.model_copy(update={"prediction_sha256": prediction_sha})
        args = harness_args(
            prefix,
            dataset_path=dataset_path,
            split=self.gold.split,
            instance_id=request.instance_id,
            predictions_path=predictions_path,
            run_id=run_id,
            test_timeout=self.test_timeout_seconds,
            report_dir=report_dir,
        )
        manifest = {"args": args, "harness": harness.model_dump(mode="json")}
        (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        exit_code, timed_out = await run_harness(
            args, cwd=run_dir, timeout=self.timeout_seconds, log_path=run_dir / "harness.log"
        )
        harness = harness.model_copy(update={"exit_code": exit_code})
        if timed_out:
            await asyncio.to_thread(remove_run_containers, run_id, run_dir)
            return finish(
                EvaluationStatus.TIMEOUT,
                f"The SWE-bench harness did not finish within {self.timeout_seconds:g}s.",
                "HARNESS_TIMED_OUT",
            )
        outcome = parse_outcome(
            run_dir=run_dir,
            report_dir=report_dir,
            run_id=run_id,
            instance_id=request.instance_id,
            exit_code=exit_code,
        )
        return finish(outcome.status, outcome.message, outcome.failure_reason, outcome)


class DisabledEvaluator:
    """Evaluator for deployments without Docker (the Cloud Run web service).

    It never loads reference data and never starts the harness; the case ends
    UNRESOLVED with no test verdict. Grade such patches locally or in GitHub Actions."""

    async def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        return failed_result(
            request,
            EvaluationStatus.UNAVAILABLE,
            "EVALUATION_DISABLED",
            "SWE-bench evaluation is disabled in this deployment; "
            "run it locally or in GitHub Actions.",
            datetime.now(UTC),
        )


def build_evaluator(settings: Settings) -> Evaluator:
    if not settings.evaluation_enabled:
        return DisabledEvaluator()
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

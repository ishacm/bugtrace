"""Machine-readable agent-evaluation reports, baseline comparison and exit policy.

Every number in a report is read from persisted investigations, workspace state
or the evaluator; a metric that was not measured is null, never estimated.
Reports are gold-free: localization arrives as already-computed metrics.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.evaluation.gold import GoldSource, ReferenceUnavailableError, modified_files
from app.evaluation.metrics import localization_metrics, suspect_files
from app.evaluation.models import LocalizationMetrics
from app.storage.models import ApprovalDecision, StoredInvestigation

SCHEMA_VERSION = 1
Mode = Literal["stub", "live"]

# Summary metrics where a lower current value than the baseline is a regression.
HIGHER_IS_BETTER = (
    "completed_reports",
    "patches_proposed",
    "resolved",
    "top1_file_accuracy",
    "top3_file_recall",
    "grounding_rate",
)
EXIT_OK, EXIT_REGRESSION, EXIT_UNAUTHORIZED_WRITE = 0, 1, 2


class ManifestCase(BaseModel):
    """A regression case: a safe identifier only, never answer data."""

    model_config = ConfigDict(extra="forbid")

    instance_id: str = Field(pattern=r"^[A-Za-z0-9_.\-]{1,200}$")
    note: str = ""


class EvalManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    description: str = ""
    cases: list[ManifestCase] = Field(min_length=1, max_length=10)


def load_manifest(path: Path) -> EvalManifest:
    return EvalManifest.model_validate_json(path.read_text(encoding="utf-8"))


class CaseReport(BaseModel):
    instance_id: str
    investigation_id: str
    status: str
    error: str | None = None
    report_status: str | None = None
    confidence: str | None = None
    tool_calls: int = 0
    iterations: int = 0
    evidence_items: int = 0
    suspects: int = 0
    claims: int = 0
    grounded_claims: int = 0
    patch_proposed: bool = False
    decision: str | None = None
    patch_applied: bool | None = None
    evaluation_status: str | None = None
    resolved: bool | None = None
    localization: LocalizationMetrics | None = None
    changed_files: list[str] = Field(default_factory=list)
    unauthorized_writes: int = 0
    duration_seconds: float | None = None


class Summary(BaseModel):
    cases: int
    completed_reports: int
    patches_proposed: int
    patches_applied: int
    evaluated: int  # harness produced a test verdict
    resolved: int
    top1_file_accuracy: float | None  # over cases with localization
    top3_file_recall: float | None
    grounding_rate: float | None  # grounded claims / claims
    unauthorized_writes: int
    tool_calls: int


class MetricDelta(BaseModel):
    metric: str
    baseline: float | None
    current: float | None
    regressed: bool


class BaselineComparison(BaseModel):
    baseline_path: str
    deltas: list[MetricDelta]
    regressions: list[str]


class RunReport(BaseModel):
    schema_version: int = SCHEMA_VERSION
    mode: Mode
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    config: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    cases: list[CaseReport]
    summary: Summary
    baseline: BaselineComparison | None = None


def unauthorized_writes(stored: StoredInvestigation, changed_files: list[str]) -> list[str]:
    """Workspace changes not covered by an approved, applied patch.

    Without an approve decision and a recorded apply, any change is unauthorized."""
    record = stored.record
    allowed: set[str] = set()
    approved = stored.approval is not None and stored.approval.decision == ApprovalDecision.APPROVE
    applied = bool(record and record.patch_result and record.patch_result.applied)
    if approved and applied and record and record.proposed_patch:
        allowed = set(record.proposed_patch.affected_files)
    return [f for f in changed_files if f not in allowed]


def case_report(
    stored: StoredInvestigation,
    changed_files: list[str],
    localization: LocalizationMetrics | None = None,
) -> CaseReport:
    record = stored.record
    report = record.final_report if record else None
    known = {e.evidence_id for e in record.evidence} if record else set()
    claims = report.claims if report else []
    evaluation = stored.evaluation
    return CaseReport(
        instance_id=stored.case.instance_id,
        investigation_id=stored.investigation_id,
        status=stored.status.value,
        error=stored.error,
        report_status=report.status if report else None,
        confidence=report.confidence.value if report else None,
        tool_calls=record.tool_call_count if record else 0,
        iterations=record.iteration if record else 0,
        evidence_items=len(known),
        suspects=len(record.suspects) if record else 0,
        claims=len(claims),
        grounded_claims=sum(1 for c in claims if known & set(c.evidence_ids)),
        patch_proposed=bool(record and record.proposed_patch),
        decision=stored.approval.decision.value if stored.approval else None,
        patch_applied=record.patch_result.applied if record and record.patch_result else None,
        evaluation_status=evaluation.status.value if evaluation else None,
        resolved=evaluation.resolved if evaluation else None,
        localization=localization or (evaluation.localization if evaluation else None),
        changed_files=changed_files,
        unauthorized_writes=len(unauthorized_writes(stored, changed_files)),
        duration_seconds=round((stored.updated_at - stored.created_at).total_seconds(), 3),
    )


def reference_localization(
    gold: GoldSource, stored: StoredInvestigation
) -> LocalizationMetrics | None:
    """Localization for a finished investigation (evaluator side, after the agent ran)."""
    try:
        reference = modified_files(gold.row(stored.case.instance_id).get("patch", ""))
    except ReferenceUnavailableError:
        return None
    record = stored.record
    candidate = record.proposed_patch.affected_files if record and record.proposed_patch else []
    return localization_metrics(reference, suspect_files(stored), candidate)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def summarize(cases: list[CaseReport]) -> Summary:
    located = [c.localization for c in cases if c.localization is not None]
    claims = sum(c.claims for c in cases)
    return Summary(
        cases=len(cases),
        completed_reports=sum(1 for c in cases if c.report_status == "COMPLETED"),
        patches_proposed=sum(1 for c in cases if c.patch_proposed),
        patches_applied=sum(1 for c in cases if c.patch_applied),
        evaluated=sum(1 for c in cases if c.resolved is not None),
        resolved=sum(1 for c in cases if c.resolved is True),
        top1_file_accuracy=_mean([float(m.top1_file_hit) for m in located]),
        top3_file_recall=_mean([m.top3_file_recall for m in located]),
        grounding_rate=round(sum(c.grounded_claims for c in cases) / claims, 4) if claims else None,
        unauthorized_writes=sum(c.unauthorized_writes for c in cases),
        tool_calls=sum(c.tool_calls for c in cases),
    )


def compare(current: Summary, baseline: Summary, baseline_path: str) -> BaselineComparison:
    deltas: list[MetricDelta] = []
    for metric in (*HIGHER_IS_BETTER, "unauthorized_writes"):
        now, before = getattr(current, metric), getattr(baseline, metric)
        if metric == "unauthorized_writes":
            regressed = now > before
        else:
            regressed = now is not None and before is not None and now < before
        deltas.append(MetricDelta(metric=metric, baseline=before, current=now, regressed=regressed))
    return BaselineComparison(
        baseline_path=baseline_path,
        deltas=deltas,
        regressions=[d.metric for d in deltas if d.regressed],
    )


def build_report(
    mode: Mode,
    cases: list[CaseReport],
    *,
    config: dict[str, str | int | float | bool | None] | None = None,
    baseline_path: Path | None = None,
) -> RunReport:
    summary = summarize(cases)
    comparison = None
    if baseline_path is not None and baseline_path.is_file():
        baseline = RunReport.model_validate_json(baseline_path.read_text(encoding="utf-8"))
        if baseline.mode == mode:
            comparison = compare(summary, baseline.summary, str(baseline_path))
    return RunReport(
        mode=mode, config=config or {}, cases=cases, summary=summary, baseline=comparison
    )


def exit_code(report: RunReport, *, fail_on_regression: bool) -> int:
    """Unauthorized writes always fail; baseline regressions fail when requested."""
    if report.summary.unauthorized_writes > 0:
        return EXIT_UNAUTHORIZED_WRITE
    if fail_on_regression and report.baseline and report.baseline.regressions:
        return EXIT_REGRESSION
    return EXIT_OK


def write_report(report: RunReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")


def report_digest(report: RunReport) -> str:
    """One-line human summary for logs (no case content)."""
    s = report.summary
    parts = [
        f"mode={report.mode}",
        f"cases={s.cases}",
        f"completed={s.completed_reports}",
        f"proposed={s.patches_proposed}",
        f"applied={s.patches_applied}",
        f"resolved={s.resolved}/{s.evaluated}",
        f"top1={s.top1_file_accuracy}",
        f"top3={s.top3_file_recall}",
        f"grounding={s.grounding_rate}",
        f"unauthorized_writes={s.unauthorized_writes}",
    ]
    if report.baseline:
        parts.append(f"regressions={json.dumps(report.baseline.regressions)}")
    return " ".join(parts)


def as_baseline(report: RunReport) -> RunReport:
    """The stored form of a baseline: summary and config only, no per-case data."""
    return report.model_copy(update={"cases": [], "baseline": None})

"""Server-side rendering of investigation views.

The page and the SSE stream render through the same Jinja partials, so a live
update and a browser refresh produce identical markup from persisted state.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi.templating import Jinja2Templates

from app.agent.models import AgentEvent
from app.api.investigations import InvestigationResponse
from app.evaluation.models import EvaluationResult, EvaluationStatus
from app.storage.models import (
    TERMINAL_STATUSES,
    ApprovalDecision,
    InvestigationStatus,
    StoredInvestigation,
)
from app.web import plain

S = InvestigationStatus

templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")

# Panels replaced as a unit on every state update; names match `data-panel` in the page.
PANELS = ("status", "suspects", "evidence", "hypothesis", "report", "proposal", "evaluation")

VerdictKind = Literal[
    "running", "awaiting", "completed", "partial", "unresolved", "failed", "cancelled"
]


@dataclass(frozen=True)
class Verdict:
    kind: VerdictKind
    title: str  # plain language, shown first
    detail: str
    label: str  # short technical/detective label, shown as secondary text


def verdict(inv: InvestigationResponse) -> Verdict:
    """Plain-language outcome derived only from persisted status, report and error."""
    status, report = inv.status, inv.report
    if status in (S.CREATED, S.PREPARING):
        return Verdict(
            "running",
            "Preparing a clean copy of the software project",
            f"Downloading {inv.case.repo} as it was before the bug was fixed.",
            "PREPARING WORKSPACE",
        )
    if status == S.AWAITING_APPROVAL:
        return Verdict(
            "awaiting",
            "Waiting for your approval",
            "BugTrace has suggested a code fix. Nothing changes unless you approve it.",
            "AWAITING APPROVAL",
        )
    if status == S.PATCHING:
        return Verdict(
            "running",
            "Applying the approved fix",
            "Applying the approved change to BugTrace's isolated copy of the project.",
            "APPLYING APPROVED PATCH",
        )
    if status == S.EVALUATING:
        return Verdict(
            "running",
            "Testing the fix",
            "Running the project's own tests against the fix in an isolated environment.",
            "EVALUATING PATCH",
        )
    if status not in TERMINAL_STATUSES:
        return Verdict(
            "running",
            "Investigating the code",
            "BugTrace is reading the code and collecting evidence.",
            "INVESTIGATION IN PROGRESS",
        )
    if status == S.FAILED:
        return Verdict(
            "failed",
            "The investigation stopped because of an error",
            inv.error or "No error message recorded.",
            "INVESTIGATION FAILED",
        )
    if status == S.CANCELLED:
        return Verdict(
            "cancelled",
            "Investigation cancelled",
            "Stopped on request. Anything shown below was gathered before cancellation.",
            "INVESTIGATION CANCELLED",
        )
    if status == S.RESOLVED:
        return Verdict(
            "completed",
            "Bug confirmed fixed",
            "The project's tests pass with the approved fix: the tests for this bug now pass "
            "and the existing tests still pass.",
            "CASE CLOSED",
        )
    if inv.evaluation is not None:
        return _evaluated_verdict(inv.evaluation)
    if inv.approval is not None and inv.approval.decision == ApprovalDecision.REJECT:
        return Verdict(
            "unresolved",
            "Suggested fix rejected",
            "The suggested fix was rejected. BugTrace's copy of the project was left unchanged.",
            "PATCH REJECTED",
        )
    if inv.approval is not None:
        result = inv.patch_result
        if result is not None and result.applied:
            return Verdict(
                "completed",
                "Fix applied, not tested",
                "The approved fix was applied to BugTrace's isolated copy (not committed). "
                "It has not been tested, so the bug is not confirmed fixed.",
                "PATCH APPLIED",
            )
        return Verdict(
            "failed",
            "The approved fix could not be applied",
            result.message if result else "No result was recorded for the approved patch.",
            "PATCH NOT APPLIED",
        )
    if report is None:
        return Verdict(
            "unresolved",
            "Investigation incomplete",
            "No explanation of the bug was produced.",
            "UNRESOLVED",
        )
    if report.status == "COMPLETED":
        no_patch = f" No safe code fix was produced: {inv.patch_error}" if inv.patch_error else ""
        return Verdict(
            "completed",
            "Likely cause found",
            f"BugTrace explained the bug with {report.confidence.lower()} confidence.{no_patch} "
            "No fix has been tested, so the bug is not confirmed fixed.",
            "ROOT CAUSE IDENTIFIED",
        )
    return Verdict(
        "partial",
        "Investigation incomplete",
        "The evidence was not strong enough for a full explanation "
        f"({report.confidence.lower()} confidence). The bug is not confirmed fixed.",
        "PARTIAL FINDINGS",
    )


def _flow_progress(inv: InvestigationResponse) -> list[tuple[str, str]]:
    """Each FLOW step as done/current/todo, from what has actually been persisted."""
    done = [
        True,
        inv.tool_call_count > 0,
        any(e.type != "issue" for e in inv.evidence),
        inv.report is not None,
        inv.proposed_patch is not None,
        inv.approval is not None,
    ]
    running = inv.status not in TERMINAL_STATUSES
    current = next((i for i, d in enumerate(done) if not d), None) if running else None
    return [
        (step, "done" if done[i] else "current" if i == current else "todo")
        for i, step in enumerate(plain.FLOW)
    ]


def _citations(inv: InvestigationResponse) -> dict[str, list[str]]:
    """Evidence ID -> the findings that cite it ("why it matters")."""
    cited: dict[str, list[str]] = {}

    def add(ids: list[str], what: str) -> None:
        for evidence_id in ids:
            if what not in cited.setdefault(evidence_id, []):
                cited[evidence_id].append(what)

    for s in sorted(inv.suspects, key=lambda s: s.rank):
        add(s.evidence_ids, f"likely cause #{s.rank}")
    if inv.current_hypothesis:
        add(inv.current_hypothesis.evidence_ids, "the current explanation")
    if inv.report:
        for c in inv.report.claims:
            add(c.evidence_ids, "the explanation of why the bug happens")
    if inv.proposed_patch:
        add(inv.proposed_patch.evidence_ids, "the suggested fix")
    return cited


def context(stored: StoredInvestigation) -> dict[str, object]:
    inv = InvestigationResponse.from_stored(stored)
    return {
        "inv": inv,
        "verdict": verdict(inv),
        "terminal": inv.status in TERMINAL_STATUSES,
        "evidence_ids": {e.evidence_id for e in inv.evidence},
        "last_seq": len(inv.events) - 1,
        "guide": plain.guide_for(inv.case),
        "status_label": plain.STATUS_LABELS[inv.status],
        "flow": _flow_progress(inv),
        "evidence_views": {e.evidence_id: plain.evidence_view(e) for e in inv.evidence},
        "citations": _citations(inv),
        "confidence_text": plain.confidence_text,
        "swe_bench_explainer": plain.SWE_BENCH_EXPLAINER,
    }


def render_panels(stored: StoredInvestigation) -> dict[str, str]:
    ctx = context(stored)
    return {name: templates.get_template(f"partials/{name}.html").render(ctx) for name in PANELS}


def evidence_sources(stored: StoredInvestigation) -> dict[str, str]:
    record = stored.record
    return {e.evidence_id: e.source for e in record.evidence} if record else {}


def render_event(event: AgentEvent, seq: int, sources: Mapping[str, str] | None = None) -> str:
    return templates.get_template("partials/event.html").render(
        event=event, seq=seq, headline=plain.event_headline(event, sources or {})
    )


def _evaluated_verdict(result: EvaluationResult) -> Verdict:
    """An applied patch that was evaluated but did not resolve the case."""
    if result.status == EvaluationStatus.UNRESOLVED:
        return Verdict(
            "unresolved",
            "Fix did not pass the tests",
            f"{result.message} The bug is not confirmed fixed.",
            "TESTS FAILED",
        )
    if result.status == EvaluationStatus.UNAVAILABLE:
        return Verdict(
            "unresolved",
            "Fix applied, not tested",
            f"{result.message} The fix is applied to BugTrace's isolated copy, but it was not "
            "tested, so the bug is not confirmed fixed. Benchmark testing runs only in a local "
            "or manual evaluation environment with Docker.",
            "NOT EVALUATED",
        )
    timed_out = result.status == EvaluationStatus.TIMEOUT
    return Verdict(
        "failed",
        "Testing the fix timed out" if timed_out else "The fix could not be tested",
        f"{result.message} No test result was produced, so the bug is not confirmed fixed.",
        "EVALUATION TIMED OUT" if timed_out else "EVALUATION ERROR",
    )

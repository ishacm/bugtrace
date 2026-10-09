"""Server-side rendering of investigation views.

The page and the SSE stream render through the same Jinja partials, so a live
update and a browser refresh produce identical markup from persisted state.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi.templating import Jinja2Templates

from app.agent.models import AgentEvent
from app.api.investigations import InvestigationResponse
from app.storage.models import (
    TERMINAL_STATUSES,
    ApprovalDecision,
    InvestigationStatus,
    StoredInvestigation,
)

S = InvestigationStatus

templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")

# Panels replaced as a unit on every state update; names match `data-panel` in the page.
PANELS = ("status", "suspects", "evidence", "hypothesis", "report", "proposal")

VerdictKind = Literal[
    "running", "awaiting", "completed", "partial", "unresolved", "failed", "cancelled"
]


@dataclass(frozen=True)
class Verdict:
    kind: VerdictKind
    title: str
    detail: str


def verdict(inv: InvestigationResponse) -> Verdict:
    """Plain-language outcome derived only from persisted status, report and error."""
    status, report = inv.status, inv.report
    if status in (S.CREATED, S.PREPARING):
        return Verdict("running", "PREPARING WORKSPACE", f"Checking out {inv.case.repo}.")
    if status == S.AWAITING_APPROVAL:
        return Verdict(
            "awaiting",
            "AWAITING APPROVAL",
            "A patch has been proposed. The repository will not change unless you approve it.",
        )
    if status == S.PATCHING:
        return Verdict(
            "running", "APPLYING APPROVED PATCH", "Applying the approved diff in the workspace."
        )
    if status not in TERMINAL_STATUSES:
        return Verdict("running", "INVESTIGATION IN PROGRESS", "The agent is gathering evidence.")
    if status == S.FAILED:
        return Verdict("failed", "INVESTIGATION FAILED", inv.error or "No error message recorded.")
    if status == S.CANCELLED:
        return Verdict(
            "cancelled",
            "INVESTIGATION CANCELLED",
            "Stopped on request. Anything shown below was gathered before cancellation.",
        )
    if status == S.RESOLVED:
        return Verdict("completed", "CASE CLOSED", "The proposed fix passed evaluation.")
    if inv.approval is not None and inv.approval.decision == ApprovalDecision.REJECT:
        return Verdict(
            "unresolved",
            "PATCH REJECTED",
            "The proposed patch was rejected. The workspace was left unchanged.",
        )
    if inv.approval is not None:
        result = inv.patch_result
        if result is not None and result.applied:
            return Verdict(
                "completed",
                "PATCH APPLIED",
                "The approved patch was applied in the investigation workspace (not committed). "
                "It has not been evaluated, so the case remains UNRESOLVED.",
            )
        return Verdict(
            "failed",
            "PATCH NOT APPLIED",
            result.message if result else "No result was recorded for the approved patch.",
        )
    if report is None:
        return Verdict("unresolved", "UNRESOLVED", "No root-cause report was produced.")
    if report.status == "COMPLETED":
        no_patch = f" No valid patch was proposed: {inv.patch_error}" if inv.patch_error else ""
        return Verdict(
            "completed",
            "ROOT CAUSE IDENTIFIED",
            f"Report completed with {report.confidence} confidence.{no_patch} "
            "No fix has been evaluated, so the case remains UNRESOLVED.",
        )
    return Verdict(
        "partial",
        "PARTIAL FINDINGS",
        f"The evidence did not support a complete root cause ({report.confidence} confidence). "
        "The case remains UNRESOLVED.",
    )


def context(stored: StoredInvestigation) -> dict[str, object]:
    inv = InvestigationResponse.from_stored(stored)
    return {
        "inv": inv,
        "verdict": verdict(inv),
        "terminal": inv.status in TERMINAL_STATUSES,
        "evidence_ids": {e.evidence_id for e in inv.evidence},
        "last_seq": len(inv.events) - 1,
    }


def render_panels(stored: StoredInvestigation) -> dict[str, str]:
    ctx = context(stored)
    return {name: templates.get_template(f"partials/{name}.html").render(ctx) for name in PANELS}


def render_event(event: AgentEvent, seq: int) -> str:
    return templates.get_template("partials/event.html").render(event=event, seq=seq)

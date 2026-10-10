"""Plain-language wording for the UI (presentation only).

Turns persisted, technical investigation data into text a non-specialist can read.
Nothing here changes what is stored or returned by the API, and nothing here is ever
passed to the agent. Curated case copy (`case_guides.json`) is written from public bug
reports only.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from app.agent.models import AgentEvent, Evidence
from app.data.models import PublicCase
from app.storage.models import InvestigationStatus

S = InvestigationStatus

GUIDES_PATH = Path(__file__).resolve().parent / "case_guides.json"

SWE_BENCH_EXPLAINER = (
    "This bug comes from SWE-bench, a benchmark built from real open-source software issues. "
    "BugTrace sees the original bug report and the code before the fix, but not the original "
    "developer's solution."
)

# Steps shown on the home page and as progress on the investigation page.
FLOW = (
    "Bug report",
    "Inspect code",
    "Collect evidence",
    "Explain cause",
    "Suggest fix",
    "Human approval",
)


@dataclass(frozen=True)
class CaseGuide:
    title: str
    project: str
    project_about: str
    summary: str
    curated: bool  # True: hand-written plain-English summary; False: excerpt of the report


@cache
def _guides() -> dict[str, dict[str, dict[str, str]]]:
    return json.loads(GUIDES_PATH.read_text(encoding="utf-8"))


_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def report_title(problem_statement: str) -> str:
    """First meaningful line of a bug report, used when no curated title exists."""
    text = _COMMENT_RE.sub("", problem_statement)
    for line in text.splitlines():
        line = line.replace("`", "").lstrip("#").strip()
        if line:
            return line if len(line) <= 110 else line[:107].rstrip() + "…"
    return "Untitled bug report"


def report_excerpt(problem_statement: str, limit: int = 280) -> str:
    """Readable start of a bug report: no HTML comments, headings, code fences or title."""
    text = _COMMENT_RE.sub("", problem_statement)
    lines = [line.strip() for line in text.splitlines()]
    kept, in_code = [], False
    for line in lines[1:]:
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not line or line.startswith("#"):
            continue
        kept.append(line.replace("`", ""))
    excerpt = " ".join(kept) or report_title(problem_statement)
    return excerpt if len(excerpt) <= limit else excerpt[: limit - 1].rstrip() + "…"


def guide_for(case: PublicCase) -> CaseGuide:
    guides = _guides()
    project = guides["projects"].get(case.repo)
    curated = guides["cases"].get(case.instance_id, {})
    name = project["name"] if project else case.repo.split("/")[-1]
    about = project["about"] if project else "an open-source software project."
    return CaseGuide(
        title=curated.get("title") or report_title(case.problem_statement),
        project=name,
        project_about=about,
        summary=curated.get("summary") or report_excerpt(case.problem_statement),
        curated="summary" in curated,
    )


# --- Status ---------------------------------------------------------------------------

STATUS_LABELS: dict[InvestigationStatus, str] = {
    S.CREATED: "Starting",
    S.PREPARING: "Preparing",
    S.INVESTIGATING: "Investigating",
    S.AWAITING_APPROVAL: "Waiting for approval",
    S.PATCHING: "Applying fix",
    S.EVALUATING: "Testing fix",
    S.RESOLVED: "Bug confirmed fixed",
    S.UNRESOLVED: "Bug not confirmed fixed",
    S.FAILED: "Stopped by an error",
    S.CANCELLED: "Cancelled",
}

# --- Timeline -------------------------------------------------------------------------

_EVENT_TEXT: dict[str, str] = {
    "case_started": "Opened the bug report",
    "repo_ready": "Prepared a clean copy of the software project",
    "plan_created": "Planned what to look at first",
    "search_started": "Searched the code for relevant logic",
    "evidence_found": "Found new evidence",
    "suspect_found": "Identified a likely cause",
    "suspect_removed": "Ruled out an earlier likely cause",
    "hypothesis_created": "Formed a first explanation",
    "hypothesis_revised": "Updated the likely explanation",
    "root_cause_found": "Explained why the bug happens",
    "patch_proposed": "Suggested a code fix",
    "approval_required": "Waiting for a person to approve the fix",
    "patch_approved": "A person approved the fix",
    "patch_rejected": "A person rejected the fix",
    "patch_applied": "Applied the fix to BugTrace's isolated copy",
    "evaluation_started": "Started testing the fix",
    "evaluation_finished": "Finished testing: bug not confirmed fixed",
    "case_closed": "Tests confirm the bug is fixed",
    "error": "Something went wrong",
}

_EVIDENCE_EVENT_TEXT: dict[str, str] = {
    "problem_statement": "Read the original bug report",
    "search_code": "Searched the code for relevant logic",
    "find_symbol": "Located a potentially related function",
    "read_file": "Read a section of the code",
    "git_log": "Checked the project's change history",
}

_ERROR_TEXT: dict[str, str] = {
    "NO VALID PATCH": "Could not produce a safe code fix",
    "PATCH NOT APPLIED": "The approved fix could not be applied",
    "INVESTIGATION FAILED": "The investigation stopped because of an error",
    "STEP RETRY": "A step was interrupted and will be retried",
}


def event_headline(event: AgentEvent, evidence_sources: Mapping[str, str]) -> str:
    """Human-readable headline for a timeline event; the raw title stays secondary."""
    if event.type == "evidence_found":
        source = evidence_sources.get(str(event.metadata.get("id", "")))
        if source in _EVIDENCE_EVENT_TEXT:
            return _EVIDENCE_EVENT_TEXT[source]
    if event.type == "suspect_found" and event.title == "PRIMARY SUSPECT":
        return "Identified the most likely cause"
    if event.type == "error":
        return _ERROR_TEXT.get(event.title, _EVENT_TEXT["error"])
    return _EVENT_TEXT.get(event.type, event.title.capitalize())


# --- Evidence -------------------------------------------------------------------------

_EVIDENCE_WHAT: dict[str, str] = {
    "problem_statement": "The original bug report",
    "search_code": "Places in the code that match a search",
    "find_symbol": "Where a function or class is defined",
    "read_file": "A section of the code BugTrace read",
    "git_log": "Recent changes to the project",
}

_EVIDENCE_WHY: dict[str, str] = {
    "issue": "It describes the behaviour BugTrace is trying to explain.",
    "code": "It shows code that may be responsible for the behaviour.",
    "git": "Recent changes can show when or how the behaviour was introduced.",
    "test": "Tests describe how the code is expected to behave.",
    "runtime": "It shows what actually happened when the code ran.",
}


@dataclass(frozen=True)
class EvidenceView:
    what: str
    where: str
    why: str


def evidence_view(e: Evidence) -> EvidenceView:
    what = _EVIDENCE_WHAT.get(e.source, "Repository check result")
    if e.type == "test" and e.source == "search_code":
        what = "Tests that match a search"
    if e.path:
        lines = ""
        if e.start_line:
            end = f"–{e.end_line}" if e.end_line and e.end_line != e.start_line else ""
            lines = f", lines {e.start_line}{end}"
        where = f"{e.path}{lines}"
    elif e.source == "problem_statement":
        where = "The bug report"
    else:
        where = "Across the project's code"
    return EvidenceView(what=what, where=where, why=e.relevance or _EVIDENCE_WHY[e.type])


def confidence_text(confidence: str) -> str:
    return {
        "HIGH": "High confidence: code evidence plus supporting tests or history.",
        "MEDIUM": "Medium confidence: backed by code evidence, but not enough supporting "
        "tests or history to be sure.",
        "LOW": "Low confidence: the evidence is thin or incomplete.",
    }.get(confidence, confidence)

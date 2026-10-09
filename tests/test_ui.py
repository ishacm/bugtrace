"""UI pages and SSE streaming, using stub persisted investigations and the stub-model backend."""

import asyncio
import html
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal, get_args

import pytest
from fastapi.testclient import TestClient

from app.agent.models import (
    AgentEvent,
    Claim,
    Confidence,
    EventType,
    Evidence,
    EvidenceType,
    FinalReport,
    Hypothesis,
    Suspect,
)
from app.agent.runner import InvestigationRecord
from app.config import Settings
from app.data.models import PublicCase
from app.data.store import write_cases
from app.main import create_app
from app.services.investigations import InvestigationService
from app.storage.models import InvestigationStatus
from app.storage.sqlite import InvestigationStore
from app.web import plain
from app.web.stream import investigation_stream
from tests.test_agent import CASE_STATEMENT
from tests.test_investigations_api import INSTANCE_ID, FakeBackend, StaticEvaluator, _assert_no_gold

S = InvestigationStatus


@pytest.fixture
def ui_env(tmp_path: Path, fixture_repo: tuple[Path, list[str]]) -> dict[str, Any]:
    origin, shas = fixture_repo
    case = PublicCase(
        instance_id=INSTANCE_ID,
        repo="owner/fixture",
        problem_statement=CASE_STATEMENT + "\n<script>alert('x')</script>",
        base_commit=shas[1],
    )
    cases_path = tmp_path / "cases.jsonl"
    write_cases(cases_path, [case])
    settings = Settings(
        _env_file=None,
        cases_path=cases_path,
        database_path=tmp_path / "db" / "bugtrace.db",
        workspaces_dir=tmp_path / "workspaces",
        checkpoint_path=tmp_path / "db" / "checkpoints.db",
        stream_poll_seconds=0.02,
    )
    return {"settings": settings, "origin": origin, "case": case, "tmp": tmp_path}


def _backend(ui_env: dict[str, Any], **kwargs: Any) -> FakeBackend:
    kwargs.setdefault("database_path", ui_env["settings"].database_path)
    return FakeBackend(ui_env["tmp"] / "workspaces", ui_env["origin"], **kwargs)


@pytest.fixture
def ui_client(ui_env: dict[str, Any]) -> Iterator[TestClient]:
    app = create_app(settings=ui_env["settings"], backend=_backend(ui_env))
    with TestClient(app) as client:
        yield client


def _store(client: TestClient) -> InvestigationStore:
    return client.app.state.investigations.store  # type: ignore[attr-defined]


# --- stub persisted investigations ------------------------------------------------

ReportKind = Literal["COMPLETED", "PARTIAL"] | None


def _record(
    inv: str, case: PublicCase, *, report: ReportKind, findings: bool
) -> InvestigationRecord:
    evidence = [
        Evidence(
            evidence_id="ev-aaaaaaaaaaaa",
            type=EvidenceType.CODE,
            source="read_file",
            path="pkg/core.py",
            start_line=14,
            end_line=15,
            excerpt="def compute_total(items):\n    return sum(items) + 1",
            summary="compute_total adds an extra 1 <b>",
        )
    ]
    suspects = (
        [
            Suspect(
                path="pkg/core.py",
                symbol="compute_total",
                rank=1,
                score=3,
                reason="Returns sum + 1",
                evidence_ids=["ev-aaaaaaaaaaaa"],
            )
        ]
        if findings
        else []
    )
    hypotheses = (
        [
            Hypothesis(
                iteration=1,
                statement="Off-by-one in compute_total",
                evidence_ids=["ev-aaaaaaaaaaaa"],
            )
        ]
        if findings
        else []
    )
    final = None
    if report is not None:
        final = FinalReport(
            investigation_id=inv,
            instance_id=case.instance_id,
            status=report,
            confidence=Confidence.MEDIUM if report == "COMPLETED" else Confidence.LOW,
            summary=f"STUB SUMMARY {report}",
            claims=[Claim(statement="compute_total adds 1", evidence_ids=["ev-aaaaaaaaaaaa"])]
            if findings
            else [],
            suspects=suspects,
            cited_evidence_ids=["ev-aaaaaaaaaaaa"] if findings else [],
            open_questions=[],
            limits_reached=[],
            tool_calls=4,
            iterations=1,
        )
    events = [
        AgentEvent(type="case_started", title="CASE OPENED", message="stub <img src=x>"),
        AgentEvent(type="evidence_found", title="NEW EVIDENCE", message="compute_total"),
    ]
    if final is not None:
        events.append(AgentEvent(type="root_cause_found", title="ROOT CAUSE", message="found"))
    return InvestigationRecord(
        investigation_id=inv,
        case=case,
        analysis=None,
        plan=None,
        evidence=evidence,
        suspects=suspects,
        hypotheses=hypotheses,
        searches_performed=[],
        files_inspected=[],
        tool_call_count=4,
        iteration=1,
        limits_reached=[],
        errors=[],
        events=events,
        final_report=final,
    )


def _seed(
    store: InvestigationStore,
    case: PublicCase,
    path: list[S],
    *,
    report: ReportKind = None,
    findings: bool = True,
    record: bool = True,
    error: str | None = None,
) -> str:
    inv = str(uuid.uuid4())
    store.create(inv, case)
    store.append_event(inv, AgentEvent(type="repo_ready", title="REPO READY", message="ok"))
    for target in path[:-1]:
        store.transition(inv, target)
    if record:
        store.save_record(_record(inv, case, report=report, findings=findings))
    store.transition(inv, path[-1], error=error)
    return inv


FINISHED = [S.PREPARING, S.INVESTIGATING, S.UNRESOLVED]


def _page(client: TestClient, inv: str) -> str:
    response = client.get(f"/investigations/{inv}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    return response.text


def _parse_sse(text: str) -> list[dict[str, Any]]:
    messages = []
    for block in text.split("\n\n"):
        fields: dict[str, Any] = {}
        for line in block.splitlines():
            if line.startswith(":"):
                continue
            name, _, value = line.partition(": ")
            fields[name] = value
        if "event" in fields:
            fields["data"] = json.loads(fields["data"])
            messages.append(fields)
    return messages


# --- pages ------------------------------------------------------------------------


def test_home_lists_cases_and_case_files(ui_client: TestClient, ui_env: dict[str, Any]) -> None:
    inv = _seed(_store(ui_client), ui_env["case"], FINISHED, report="COMPLETED")
    html = ui_client.get("/").text
    assert INSTANCE_ID in html
    assert "owner/fixture" in html
    assert f'data-open-case="{INSTANCE_ID}"' in html
    assert f"/investigations/{inv}" in html
    assert "<script>alert" not in html


def test_home_without_seeded_cases(ui_env: dict[str, Any]) -> None:
    settings = ui_env["settings"].model_copy(update={"cases_path": ui_env["tmp"] / "none.jsonl"})
    with TestClient(create_app(settings=settings, backend=_backend(ui_env))) as client:
        assert "make seed" in client.get("/").text


def test_static_assets_served(ui_client: TestClient) -> None:
    for name in ("bugtrace.css", "investigation.js", "home.js"):
        assert ui_client.get(f"/static/{name}").status_code == 200


def test_completed_report_page_shows_persisted_findings(
    ui_client: TestClient, ui_env: dict[str, Any]
) -> None:
    inv = _seed(_store(ui_client), ui_env["case"], FINISHED, report="COMPLETED")
    html = _page(ui_client, inv)
    assert 'data-terminal="true"' in html
    assert "ROOT CAUSE IDENTIFIED" in html
    assert "UNRESOLVED" in html  # lifecycle status stays visible
    assert "STUB SUMMARY COMPLETED" in html
    assert "PRIMARY SUSPECT" in html and "compute_total" in html
    assert 'id="ev-aaaaaaaaaaaa"' in html and 'href="#ev-aaaaaaaaaaaa"' in html
    assert "Off-by-one in compute_total" in html
    assert "CASE OPENED" in html and 'data-seq="0"' in html
    assert "data-cancel" not in html


def test_partial_report_page(ui_client: TestClient, ui_env: dict[str, Any]) -> None:
    inv = _seed(_store(ui_client), ui_env["case"], FINISHED, report="PARTIAL")
    html = _page(ui_client, inv)
    assert "PARTIAL FINDINGS" in html
    assert "STUB SUMMARY PARTIAL" in html
    assert "ROOT CAUSE IDENTIFIED" not in html


def test_unresolved_without_findings_fabricates_nothing(
    ui_client: TestClient, ui_env: dict[str, Any]
) -> None:
    inv = _seed(_store(ui_client), ui_env["case"], FINISHED, report=None, findings=False)
    html = _page(ui_client, inv)
    assert "No explanation of the bug was produced." in html
    assert "No likely causes were identified." in html
    assert "No explanation was formed." in html
    assert "PRIMARY SUSPECT" not in html
    assert "Grounded claims" not in html


def test_failed_page_shows_error(ui_client: TestClient, ui_env: dict[str, Any]) -> None:
    inv = _seed(
        _store(ui_client),
        ui_env["case"],
        [S.PREPARING, S.FAILED],
        record=False,
        error="WorkspaceError: clone failed",
    )
    html = _page(ui_client, inv)
    assert "INVESTIGATION FAILED" in html
    assert "WorkspaceError: clone failed" in html
    assert "badge-failed" in html
    assert "No evidence was collected." in html


def test_cancelled_page(ui_client: TestClient, ui_env: dict[str, Any]) -> None:
    inv = _seed(_store(ui_client), ui_env["case"], [S.PREPARING, S.INVESTIGATING, S.CANCELLED])
    html = _page(ui_client, inv)
    assert "INVESTIGATION CANCELLED" in html
    assert "badge-cancelled" in html
    assert "compute_total" in html  # findings gathered before cancellation remain visible


def test_running_page_is_live_and_cancellable(
    ui_client: TestClient, ui_env: dict[str, Any]
) -> None:
    store = _store(ui_client)
    inv = str(uuid.uuid4())
    store.create(inv, ui_env["case"])
    store.transition(inv, S.PREPARING)
    html = _page(ui_client, inv)
    assert 'data-terminal="false"' in html
    assert 'data-last-seq="-1"' in html
    assert "PREPARING WORKSPACE" in html
    assert "data-cancel" in html
    assert "No likely causes yet." in html
    assert "No events recorded yet." in html
    assert "Root-cause report" not in html and "What it found" not in html


def test_page_escapes_untrusted_text(ui_client: TestClient, ui_env: dict[str, Any]) -> None:
    inv = _seed(_store(ui_client), ui_env["case"], FINISHED, report="COMPLETED")
    html = _page(ui_client, inv)
    assert "<script>alert" not in html and "&lt;script&gt;" in html
    assert "<img src=x>" not in html
    assert "adds an extra 1 <b>" not in html


def test_home_leads_with_plain_language_and_hides_ids(
    ui_client: TestClient, ui_env: dict[str, Any]
) -> None:
    page = ui_client.get("/").text
    assert "investigates real software bugs, collects evidence, explains the likely cause" in page
    for step in plain.FLOW:
        assert f"<li>{step}</li>" in page
    assert "SWE-bench, a benchmark built from real open-source software issues" in page
    # Uncurated case: the title comes from the report's first line, without markdown.
    title = "compute_total returns one more than the sum of its items."
    assert f'<h3 class="case-title">{title}</h3>' in page
    # The SWE-bench ID, repository and commit sit inside "Technical details", after the title.
    card = page[page.index('<article class="case-card">') :]
    details = card[card.index("<summary>Technical details</summary>") :]
    assert card.index(title) < card.index("Technical details")
    assert INSTANCE_ID in details.split("</details>")[0]
    assert "owner/fixture" in details.split("</details>")[0]
    assert "Investigate this bug" in page
    assert "How it works technically" in page


def test_curated_guides_match_seeded_cases_and_stay_display_only() -> None:
    root = Path(__file__).resolve().parent.parent
    seeded = {
        json.loads(line)["instance_id"]
        for line in (root / "data" / "cases.jsonl").read_text().splitlines()
        if line.strip()
    }
    guides = json.loads(plain.GUIDES_PATH.read_text())
    assert set(guides["cases"]) <= seeded
    for entry in guides["cases"].values():
        assert set(entry) == {"title", "summary"}  # no room for reference-fix fields
    case = PublicCase(
        instance_id="astropy__astropy-12907",
        repo="astropy/astropy",
        problem_statement="Modeling's `separability_matrix` ...",
        base_commit="a" * 40,
    )
    guide = plain.guide_for(case)
    assert guide.curated and guide.project == "Astropy"
    assert guide.project_about == "an open-source Python library for astronomy."
    assert "separability_matrix" not in guide.title  # plain title, not the raw report title
    # Display-only: no agent-side or MCP module loads the curated copy or the wording module.
    for package in ("agent", "mcp_servers", "workspace", "data", "evaluation"):
        for source in (root / "app" / package).rglob("*.py"):
            text = source.read_text()
            assert "case_guides" not in text and "app.web" not in text, source


def test_report_excerpt_skips_template_noise() -> None:
    statement = (
        "Title line\n<!-- hidden template -->\n### Description\n```python\ncode()\n```\n"
        "The `formats` option is ignored."
    )
    assert plain.report_title(statement) == "Title line"
    assert plain.report_excerpt(statement) == "The formats option is ignored."
    assert plain.report_title("<!-- only -->\n\n") == "Untitled bug report"


def test_timeline_and_status_use_plain_language(
    ui_client: TestClient, ui_env: dict[str, Any]
) -> None:
    inv = _seed(_store(ui_client), ui_env["case"], FINISHED, report="PARTIAL")
    page = _page(ui_client, inv)
    event = page[page.index('data-seq="0"') :].split("</li>")[0]
    headline = "Prepared a clean copy of the software project"
    assert headline in event and "REPO READY" in event  # raw title stays, as secondary text
    assert event.index(headline) < event.index("REPO READY")
    assert 'class="event-headline">repo_ready' not in page
    assert "Bug not confirmed fixed" in page and "Investigation incomplete" in page
    assert "Repository checks" in page and "Tool calls</dt>" not in page


@pytest.mark.parametrize(
    ("source", "title", "expected"),
    [
        ("search_code", "NEW EVIDENCE", "Searched the code for relevant logic"),
        ("find_symbol", "NEW EVIDENCE", "Located a potentially related function"),
        ("problem_statement", "NEW EVIDENCE", "Read the original bug report"),
        ("unknown_tool", "NEW EVIDENCE", "Found new evidence"),
    ],
)
def test_evidence_events_are_described_by_what_was_checked(
    source: str, title: str, expected: str
) -> None:
    event = AgentEvent(type="evidence_found", title=title, message="m", metadata={"id": "ev-1"})
    assert plain.event_headline(event, {"ev-1": source}) == expected


def test_every_event_type_has_plain_wording() -> None:
    for event_type in get_args(EventType):
        event = AgentEvent(type=event_type, title="RAW TITLE", message="m")
        headline = plain.event_headline(event, {})
        assert headline and headline != "RAW TITLE" and event_type not in headline
    error = AgentEvent(type="error", title="NO VALID PATCH", message="m")
    assert plain.event_headline(error, {}) == "Could not produce a safe code fix"


def test_evidence_explains_what_where_and_why(
    ui_client: TestClient, ui_env: dict[str, Any]
) -> None:
    inv = _seed(_store(ui_client), ui_env["case"], FINISHED, report="COMPLETED")
    page = _page(ui_client, inv)
    item = page[page.index('id="ev-aaaaaaaaaaaa"') :].split("</li>")[0]
    assert "A section of the code BugTrace read" in item
    assert "Where:</span> pkg/core.py, lines 14–15" in item
    assert "Why it matters:" in item and "Supports likely cause #1" in item
    # The evidence ID and raw excerpt are secondary, inside a disclosure.
    disclosed = item[item.index("<summary>Show the evidence</summary>") :]
    assert "ev-aaaaaaaaaaaa" in disclosed and "def compute_total" in disclosed
    assert "Why this bug happens" in page and "Why BugTrace thinks this" in page
    assert "Likely causes" in page and "Most likely cause" in page


def test_page_missing_and_invalid_ids(ui_client: TestClient) -> None:
    assert ui_client.get(f"/investigations/{uuid.uuid4()}").status_code == 404
    assert ui_client.get("/investigations/not-a-uuid").status_code == 422


# --- SSE ----------------------------------------------------------------------------


def test_sse_replays_persisted_events_then_ends(
    ui_client: TestClient, ui_env: dict[str, Any]
) -> None:
    store = _store(ui_client)
    inv = _seed(store, ui_env["case"], FINISHED, report="COMPLETED")
    response = ui_client.get(f"/api/investigations/{inv}/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    messages = _parse_sse(response.text)

    timeline = [m for m in messages if m["event"] == "timeline"]
    persisted = store.get(inv).events
    assert [int(m["id"]) for m in timeline] == list(range(len(persisted)))
    assert [m["data"]["event"]["title"] for m in timeline] == [e.title for e in persisted]
    assert "&lt;img src=x&gt;" in timeline[1]["data"]["html"]

    states = [m for m in messages if m["event"] == "state"]
    assert len(states) == 1 and states[0]["data"]["terminal"] is True
    panels = states[0]["data"]["panels"]
    assert set(panels) == {
        "status",
        "suspects",
        "evidence",
        "hypothesis",
        "report",
        "proposal",
        "evaluation",
    }
    assert "STUB SUMMARY COMPLETED" in panels["report"]
    assert messages[-1]["event"] == "end"
    assert messages[-1]["data"]["status"] == "UNRESOLVED"


def test_sse_resumes_after_last_event_id(ui_client: TestClient, ui_env: dict[str, Any]) -> None:
    inv = _seed(_store(ui_client), ui_env["case"], FINISHED, report="COMPLETED")
    url = f"/api/investigations/{inv}/events"

    def ids(**kwargs: Any) -> list[int]:
        messages = _parse_sse(ui_client.get(url, **kwargs).text)
        return [int(m["id"]) for m in messages if m["event"] == "timeline"]

    total = len(_store(ui_client).get(inv).events)
    assert ids(headers={"Last-Event-ID": "1"}) == list(range(2, total))
    assert ids(params={"after": 2}) == list(range(3, total))
    # Browser reconnects keep the original query; the header must win.
    assert ids(params={"after": 0}, headers={"Last-Event-ID": "2"}) == list(range(3, total))
    assert ids(headers={"Last-Event-ID": "999"}) == []
    # A fully caught-up client still gets the current state and the end marker.
    events = [m["event"] for m in _parse_sse(ui_client.get(url, params={"after": 999}).text)]
    assert events == ["state", "end"]


def test_sse_rejects_bad_requests(ui_client: TestClient, ui_env: dict[str, Any]) -> None:
    missing = uuid.uuid4()
    assert ui_client.get(f"/api/investigations/{missing}/events").status_code == 404
    assert ui_client.get("/api/investigations/nope/events").status_code == 422
    inv = _seed(_store(ui_client), ui_env["case"], FINISHED)
    url = f"/api/investigations/{inv}/events"
    assert ui_client.get(url, headers={"Last-Event-ID": "abc"}).status_code == 422
    assert ui_client.get(url, params={"after": -5}).status_code == 422


def test_sse_follows_live_run_to_final_state(ui_client: TestClient) -> None:
    response = ui_client.post("/api/investigations", json={"instance_id": INSTANCE_ID})
    inv = response.json()["investigation_id"]
    messages = _parse_sse(ui_client.get(f"/api/investigations/{inv}/events").text)
    final = ui_client.get(f"/api/investigations/{inv}").json()

    assert final["status"] == "UNRESOLVED"
    timeline = [m["data"]["event"] for m in messages if m["event"] == "timeline"]
    assert timeline == final["events"]  # exactly the persisted events, in order, once each
    state = [m for m in messages if m["event"] == "state"][-1]["data"]
    assert state["status"] == "UNRESOLVED" and state["terminal"] is True
    assert final["report"]["summary"].split("\n")[0][:40] in html.unescape(
        state["panels"]["report"]
    )
    assert messages[-1]["event"] == "end"
    for message in messages:
        _assert_no_gold(message["data"])

    # A refresh after the run renders the same final state from persistence.
    page = _page(ui_client, inv)
    assert f'data-last-seq="{len(final["events"]) - 1}"' in page
    assert 'data-terminal="true"' in page


def _service(ui_env: dict[str, Any], **backend_kwargs: Any) -> InvestigationService:
    settings: Settings = ui_env["settings"]
    return InvestigationService(
        store=InvestigationStore(settings.database_path),
        backend=_backend(ui_env, **backend_kwargs),
        cases_path=settings.cases_path,
        max_iterations=settings.agent_max_iterations,
        max_tool_calls=settings.agent_max_tool_calls,
        checkpoint_path=settings.checkpoint_path,
        evaluator=StaticEvaluator(),
    )


def test_stream_delivers_updates_while_running(ui_env: dict[str, Any]) -> None:
    async def scenario() -> list[dict[str, Any]]:
        service = _service(ui_env, prepare_delay=0.2)
        inv = service.start(INSTANCE_ID).investigation_id

        async def connected() -> bool:
            return False

        stream = investigation_stream(
            service, inv, after=-1, is_disconnected=connected, poll_seconds=0.02
        )
        return _parse_sse("".join([chunk async for chunk in stream]))

    messages = asyncio.run(scenario())
    states = [m["data"] for m in messages if m["event"] == "state"]
    assert states[0]["status"] == "PREPARING" and states[0]["terminal"] is False
    assert "INVESTIGATING" in {s["status"] for s in states}
    assert states[-1]["status"] == "UNRESOLVED"
    seqs = [int(m["id"]) for m in messages if m["event"] == "timeline"]
    assert seqs == list(range(len(seqs)))


def test_client_disconnect_does_not_stop_investigation(ui_env: dict[str, Any]) -> None:
    async def scenario() -> tuple[list[str], S, bool]:
        service = _service(ui_env, prepare_delay=0.2)
        inv = service.start(INSTANCE_ID).investigation_id
        gone = False

        async def disconnected() -> bool:
            return gone

        stream = investigation_stream(
            service, inv, after=-1, is_disconnected=disconnected, poll_seconds=0.02
        )
        received = [await anext(stream), await anext(stream)]  # retry hint + first state
        gone = True
        received += [chunk async for chunk in stream]

        # A second client that is dropped mid-stream (response cancelled) also leaves the run alone.
        other = investigation_stream(
            service, inv, after=-1, is_disconnected=disconnected, poll_seconds=0.02
        )
        await other.aclose()

        final = await service.wait(inv)
        return (
            received,
            final.status,
            final.record is not None and final.record.final_report is not None,
        )

    received, status, has_report = asyncio.run(scenario())
    assert len(received) == 2 and "event: state" in received[1]
    assert status == S.UNRESOLVED
    assert has_report


def test_failed_run_streams_error_event_before_end(ui_env: dict[str, Any]) -> None:
    async def scenario() -> list[dict[str, Any]]:
        service = _service(ui_env)
        service.backend.fail_prepare = True  # type: ignore[attr-defined]
        inv = service.start(INSTANCE_ID).investigation_id

        async def connected() -> bool:
            return False

        stream = investigation_stream(
            service, inv, after=-1, is_disconnected=connected, poll_seconds=0.02
        )
        return _parse_sse("".join([chunk async for chunk in stream]))

    messages = asyncio.run(scenario())
    timeline = [m["data"]["event"] for m in messages if m["event"] == "timeline"]
    assert timeline[-1]["type"] == "error"
    assert "clone failed" in timeline[-1]["message"]
    assert messages[-1] == {"event": "end", "data": {"status": "FAILED"}}
    assert (
        "INVESTIGATION FAILED"
        in [m for m in messages if m["event"] == "state"][-1]["data"]["panels"]["status"]
    )

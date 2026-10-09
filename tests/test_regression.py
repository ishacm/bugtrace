"""End-to-end regression suite over `tests/regression/scenarios.json`.

Every scenario drives the full state machine through the real API, service, store,
graph, privileged patch tools and evaluator boundary, with a stub model, the
fixture repo and a static evaluator or fake harness subprocess (no Gemini, Docker
or network). The scenarios run once per module; the tests below assert one
property each over all of them. Set REGRESSION_REPORT_PATH to also write the JSON
report (CI uploads it as an artifact).
"""

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agent.models import AgentEvent
from app.agent.patches import patch_sha256
from app.api.investigations import InvestigationResponse
from app.config import Settings
from app.data.store import write_cases
from app.data.swebench import public_case_from_row
from app.evaluation.evaluator import EvaluationRefusedError, build_request
from app.evaluation.models import EvaluationResult, EvaluationStatus, PassFailCounts
from app.evaluation.report import (
    EXIT_OK,
    EXIT_REGRESSION,
    EXIT_UNAUTHORIZED_WRITE,
    RunReport,
    build_report,
    case_report,
    compare,
    exit_code,
    unauthorized_writes,
    write_report,
)
from app.main import create_app
from app.storage.models import (
    TERMINAL_STATUSES,
    TRANSITIONS,
    InvestigationStatus,
)
from app.storage.sqlite import InvestigationStore
from app.web.render import render_panels, verdict
from tests.test_agent import CASE_STATEMENT, FakeModel
from tests.test_evaluation import FIX_MARKER, _evaluator, _reference_row
from tests.test_investigations_api import INSTANCE_ID, FakeBackend, StaticEvaluator, _wait_for
from tests.test_patches import GOOD_DIFF, ROOT, STALE_DIFF, _git, _patch_model
from tests.test_ui import _parse_sse

S = InvestigationStatus
MANIFEST = Path(__file__).resolve().parent / "regression" / "scenarios.json"
STUB_BASELINE = ROOT / "evals" / "baselines" / "stub.json"
SCENARIOS: list[dict[str, Any]] = json.loads(MANIFEST.read_text())["scenarios"]
DIFFS = {"good": GOOD_DIFF, "stale": STALE_DIFF}


@dataclass
class Outcome:
    scenario: dict[str, Any]
    investigation_id: str
    body: dict[str, Any]
    page: str
    sse: list[dict[str, Any]]
    path: list[str]
    changed_files: list[str]
    head_before: str
    head_after: str
    clean_while_awaiting: bool | None
    requests: list[Any] = field(default_factory=list)


@dataclass
class Suite:
    settings: Settings
    origin: Path
    outcomes: dict[str, Outcome]
    source_status_before: str
    source_status_after: str


def _model_factory(kind: str, diff: str) -> Any:
    make = _patch_model(diff)
    if kind == "happy":
        return make

    def partial() -> FakeModel:
        model = make()
        del model.handlers[next(k for k in model.handlers if k.__name__ == "RootCauseDraft")]
        return model

    return partial


def _record_path(store: InvestigationStore, path: dict[str, list[str]]) -> None:
    """Wrap the store's status-changing methods to log every status actually stored."""
    for name in ("create", "transition", "record_decision", "record_evaluation"):
        original = getattr(store, name)

        def wrapped(investigation_id: str, *args: Any, _original: Any = original, **kw: Any):
            stored = _original(investigation_id, *args, **kw)
            path.setdefault(investigation_id, []).append(stored.status.value)
            return stored

        setattr(store, name, wrapped)


def _run_scenario(scenario: dict[str, Any], settings: Settings, origin: Path, tmp: Path) -> Outcome:
    spec = scenario["evaluator"]
    if spec["kind"] == "harness":
        row = _reference_row(base_commit=_case_base(settings))
        evaluator: Any = _evaluator(tmp / scenario["name"], row)
    elif spec.get("error"):
        evaluator = StaticEvaluator(error=RuntimeError("evaluator crashed GOLD"))
    else:
        evaluator = StaticEvaluator(EvaluationStatus(spec["status"]))
    backend = FakeBackend(
        tmp / "workspaces",
        origin,
        database_path=settings.database_path,
        model_factory=_model_factory(scenario["model"], DIFFS[scenario["diff"]]),
    )
    paths: dict[str, list[str]] = {}
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("FAKE_HARNESS_MODE", spec.get("mode", "grade"))
        mp.setenv("FAKE_HARNESS_FIX", FIX_MARKER)
        with TestClient(create_app(settings=settings, backend=backend, evaluator=evaluator)) as c:
            service = c.app.state.investigations  # type: ignore[attr-defined]
            _record_path(service.store, paths)
            inv = c.post("/api/investigations", json={"instance_id": INSTANCE_ID}).json()[
                "investigation_id"
            ]
            body = _wait_for(c, inv, TERMINAL_STATUSES | {S.AWAITING_APPROVAL})
            repo = backend.manager.repo_dir(inv)
            head_before = _git(repo, "rev-parse", "HEAD")
            clean = None
            if body["status"] == "AWAITING_APPROVAL":
                clean = _git(repo, "status", "--porcelain") == ""
                decision = c.post(
                    f"/api/investigations/{inv}/decision", json={"decision": scenario["decision"]}
                )
                assert decision.status_code == 200, decision.text
                body = _wait_for(c, inv, TERMINAL_STATUSES)
            # Let the resumed graph finish persisting after a reject (status is already final).
            _settle(service, inv)
            body = c.get(f"/api/investigations/{inv}").json()
            page = c.get(f"/investigations/{inv}").text
            sse = _parse_sse(c.get(f"/api/investigations/{inv}/events").text)
            changed = backend.manager.changed_files(inv)
            head_after = _git(repo, "rev-parse", "HEAD")
    return Outcome(
        scenario=scenario,
        investigation_id=inv,
        body=body,
        page=page,
        sse=sse,
        path=paths[inv],
        changed_files=changed,
        head_before=head_before,
        head_after=head_after,
        clean_while_awaiting=clean,
        requests=getattr(evaluator, "requests", []),
    )


def _settle(service: Any, inv: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while inv in service._tasks and time.monotonic() < deadline:
        time.sleep(0.02)


def _case_base(settings: Settings) -> str:
    return json.loads(settings.cases_path.read_text().splitlines()[0])["base_commit"]


@pytest.fixture(scope="module")
def suite(tmp_path_factory: pytest.TempPathFactory, fixture_repo: tuple[Path, list[str]]) -> Suite:
    origin, shas = fixture_repo
    tmp = tmp_path_factory.mktemp("regression")
    case = public_case_from_row(
        {
            "instance_id": INSTANCE_ID,
            "repo": "owner/fixture",
            "problem_statement": CASE_STATEMENT,
            "base_commit": shas[1],
            "patch": "GOLD_PATCH",
            "FAIL_TO_PASS": '["GOLD_F2P"]',
        }
    )
    cases_path = tmp / "cases.jsonl"
    write_cases(cases_path, [case])
    settings = Settings(
        _env_file=None,
        cases_path=cases_path,
        database_path=tmp / "db" / "bugtrace.db",
        checkpoint_path=tmp / "db" / "checkpoints.db",
        workspaces_dir=tmp / "workspaces",
        stream_poll_seconds=0.02,
    )
    before = _git(ROOT, "status", "--porcelain")
    outcomes = {s["name"]: _run_scenario(s, settings, origin, tmp) for s in SCENARIOS}
    after = _git(ROOT, "status", "--porcelain")
    return Suite(settings, origin, outcomes, before, after)


def _stored(suite: Suite, outcome: Outcome) -> Any:
    return InvestigationStore(suite.settings.database_path).get(outcome.investigation_id)


# --- state machine -------------------------------------------------------------------------------


@pytest.mark.parametrize("name", [s["name"] for s in SCENARIOS])
def test_scenario_follows_expected_lifecycle(suite: Suite, name: str) -> None:
    outcome = suite.outcomes[name]
    expected = outcome.scenario
    assert outcome.path == expected["path"], (name, outcome.path, outcome.body["error"])
    assert outcome.body["status"] == expected["path"][-1]
    evaluation = outcome.body["evaluation"]
    assert (evaluation or {}).get("status") == expected["evaluation_status"]
    if expected["last_event"]:
        service_titles = [e["title"] for e in outcome.body["events"]]
        assert expected["last_event"] in service_titles
        if evaluation:
            assert service_titles[-1] == expected["last_event"]


def test_every_observed_transition_is_legal(suite: Suite) -> None:
    for outcome in suite.outcomes.values():
        steps = [S(s) for s in outcome.path]
        assert steps[0] == S.CREATED
        for current, target in zip(steps, steps[1:], strict=False):
            assert target in TRANSITIONS[current], (outcome.scenario["name"], current, target)
        assert steps[-1] in TERMINAL_STATUSES


def test_resolved_only_from_harness_verdict(suite: Suite) -> None:
    for outcome in suite.outcomes.values():
        evaluation = outcome.body["evaluation"]
        if outcome.body["status"] == "RESOLVED":
            assert evaluation["status"] == "RESOLVED" and evaluation["resolved"] is True
        elif evaluation is not None:
            assert evaluation["resolved"] is not True


# --- evidence grounding --------------------------------------------------------------------------


def test_claims_suspects_and_patches_cite_recorded_evidence(suite: Suite) -> None:
    for outcome in suite.outcomes.values():
        body = outcome.body
        known = {e["evidence_id"] for e in body["evidence"]}
        assert known, outcome.scenario["name"]
        report = body["report"]
        for claim in report["claims"]:
            assert claim["evidence_ids"] and set(claim["evidence_ids"]) <= known
        assert set(report["cited_evidence_ids"]) <= known
        for suspect in body["suspects"]:
            assert set(suspect["evidence_ids"]) <= known
            assert suspect["path"] != "pkg/invented.py"  # never a file without evidence
        if report["status"] == "COMPLETED":
            assert report["claims"]
        if body["proposed_patch"]:
            ids = body["proposed_patch"]["evidence_ids"]
            assert ids and set(ids) <= known  # invented id from the stub was dropped


# --- approval before mutation, hash binding, workspace isolation ---------------------------------


def test_workspace_unchanged_until_approval(suite: Suite) -> None:
    for outcome in suite.outcomes.values():
        approved = (outcome.body["approval"] or {}).get("decision") == "approve"
        if outcome.clean_while_awaiting is not None:
            assert outcome.clean_while_awaiting
        if approved:
            assert outcome.changed_files == ["pkg/core.py"]
        else:
            assert outcome.changed_files == [], outcome.scenario["name"]
        assert outcome.head_before == outcome.head_after  # never committed


def test_no_unauthorized_writes_and_source_tree_untouched(suite: Suite) -> None:
    for outcome in suite.outcomes.values():
        assert unauthorized_writes(_stored(suite, outcome), outcome.changed_files) == []
    assert suite.source_status_after == suite.source_status_before


def test_each_investigation_has_its_own_workspace(suite: Suite) -> None:
    ids = [o.investigation_id for o in suite.outcomes.values()]
    assert len(set(ids)) == len(ids)
    root = suite.settings.workspaces_dir.resolve()
    for inv in ids:
        assert (root / inv / "repo").is_dir()


def test_patch_hash_binds_proposal_approval_apply_and_evaluation(suite: Suite) -> None:
    for outcome in suite.outcomes.values():
        patch = outcome.body["proposed_patch"]
        if patch is None:
            assert outcome.body["approval"] is None
            continue
        assert patch_sha256(patch["diff"]) == patch["sha256"]
        assert outcome.body["approval"]["patch_sha256"] == patch["sha256"]
        for request in outcome.requests:
            assert request.patch_sha256 == patch["sha256"]
            assert request.candidate_diff == patch["diff"]
        if outcome.body["evaluation"]:
            assert outcome.body["evaluation"]["patch_sha256"] == patch["sha256"]


def test_tampered_patch_cannot_be_evaluated(suite: Suite) -> None:
    stored = _stored(suite, suite.outcomes["approved_resolved"])
    evaluating = stored.model_copy(update={"status": S.EVALUATING})
    build_request(evaluating, ["pkg/core.py"])  # the genuine state is accepted
    patch = stored.record.proposed_patch
    tampered = patch.model_copy(update={"diff": patch.diff.replace("sum(items)", "0")})
    record = stored.record.model_copy(update={"proposed_patch": tampered})
    with pytest.raises(EvaluationRefusedError, match="does not match"):
        build_request(evaluating.model_copy(update={"record": record}), ["pkg/core.py"])


# --- gold-data isolation -------------------------------------------------------------------------


def test_no_gold_data_in_api_pages_streams_or_agent_state(suite: Suite) -> None:
    gold_keys = {"patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS", "hints_text", "gold"}

    def keys(value: Any) -> set[str]:
        if isinstance(value, dict):
            return set(value) | set().union(*(keys(v) for v in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(v) for v in value)) if value else set()
        return set()

    for outcome in suite.outcomes.values():
        assert not gold_keys & keys(outcome.body)
        for text in (json.dumps(outcome.body), outcome.page, json.dumps(outcome.sse)):
            assert "GOLD" not in text and "helpers_gold" not in text
    checkpoints = suite.settings.checkpoint_path.read_bytes()
    assert b"GOLD" not in checkpoints and b"helpers_gold" not in checkpoints
    for outcome in suite.outcomes.values():
        record = _stored(suite, outcome).record
        assert record is not None and "evaluation" not in type(record).model_fields
        assert "GOLD" not in record.model_dump_json()


# --- persistence and SSE reconstruction ----------------------------------------------------------


def test_state_survives_restart(suite: Suite) -> None:
    backend = FakeBackend(
        suite.settings.workspaces_dir, suite.origin, database_path=suite.settings.database_path
    )
    app = create_app(settings=suite.settings, backend=backend, evaluator=StaticEvaluator())
    with TestClient(app) as client:
        for outcome in suite.outcomes.values():
            after = client.get(f"/api/investigations/{outcome.investigation_id}").json()
            assert after == outcome.body, outcome.scenario["name"]


def test_sse_reconstructs_persisted_timeline_and_final_panels(suite: Suite) -> None:
    for outcome in suite.outcomes.values():
        stored = _stored(suite, outcome)
        timeline = [m for m in outcome.sse if m["event"] == "timeline"]
        assert [int(m["id"]) for m in timeline] == list(range(len(stored.events)))
        assert [m["data"]["event"]["title"] for m in timeline] == [e.title for e in stored.events]
        states = [m for m in outcome.sse if m["event"] == "state"]
        assert states[-1]["data"]["status"] == outcome.body["status"]
        assert states[-1]["data"]["terminal"] is True
        assert states[-1]["data"]["panels"] == render_panels(stored)
        assert outcome.sse[-1]["event"] == "end"
        assert verdict(InvestigationResponse.from_stored(stored)).title in outcome.page


# --- evaluator result mapping --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "resolved", "final", "title"),
    [
        (EvaluationStatus.RESOLVED, True, S.RESOLVED, "CASE CLOSED"),
        (EvaluationStatus.RESOLVED, None, S.UNRESOLVED, "EVALUATION ERROR"),  # no verdict flag
        (EvaluationStatus.UNRESOLVED, False, S.UNRESOLVED, "TESTS FAILED"),
        (EvaluationStatus.ERROR, None, S.UNRESOLVED, "EVALUATION ERROR"),
        (EvaluationStatus.TIMEOUT, None, S.UNRESOLVED, "EVALUATION TIMED OUT"),
        (EvaluationStatus.UNAVAILABLE, None, S.UNRESOLVED, "NOT EVALUATED"),
    ],
)
def test_evaluator_result_maps_to_case_status_and_verdict(
    suite: Suite, status: EvaluationStatus, resolved: bool | None, final: S, title: str
) -> None:
    store = InvestigationStore(suite.settings.database_path)
    inv = str(uuid.uuid4())
    store.create(inv, _stored(suite, suite.outcomes["approved_resolved"]).case)
    for step in (S.PREPARING, S.INVESTIGATING, S.AWAITING_APPROVAL, S.PATCHING, S.EVALUATING):
        store.transition(inv, step)
    now = datetime.now(UTC)
    result = EvaluationResult(
        status=status,
        resolved=resolved,
        message="mapped",
        patch_id="p",
        patch_sha256="0" * 64,
        target_tests=PassFailCounts(passed=1, failed=0) if resolved is not None else None,
        started_at=now,
        finished_at=now,
        duration_seconds=0,
    )
    stored = store.record_evaluation(
        inv, result, event=AgentEvent(type="evaluation_finished", title="x", message="x")
    )
    assert stored.status == final
    assert stored.evaluation == result
    # The verdict for a closed case comes from the persisted result, not the event.
    shown = verdict(InvestigationResponse.from_stored(stored)).title
    assert shown == title


# --- machine-readable report and baseline --------------------------------------------------------


def _stub_report(suite: Suite) -> RunReport:
    cases = [case_report(_stored(suite, o), o.changed_files) for o in suite.outcomes.values()]
    return build_report(
        "stub", cases, config={"scenarios": len(SCENARIOS)}, baseline_path=STUB_BASELINE
    )


def test_report_artifact(suite: Suite) -> None:
    """Writes the JSON report when REGRESSION_REPORT_PATH is set (CI artifact)."""
    report = _stub_report(suite)
    RunReport.model_validate_json(report.model_dump_json())  # round-trips
    target = os.environ.get("REGRESSION_REPORT_PATH")
    if target:
        write_report(report, Path(target))


def test_stub_report_matches_committed_baseline(suite: Suite) -> None:
    report = _stub_report(suite)
    summary = report.summary
    assert summary.cases == len(SCENARIOS)
    assert summary.unauthorized_writes == 0
    assert summary.resolved == sum(1 for s in SCENARIOS if s["path"][-1] == "RESOLVED")
    assert report.baseline is not None, f"missing baseline {STUB_BASELINE}"
    assert report.baseline.regressions == []
    assert exit_code(report, fail_on_regression=True) == EXIT_OK
    baseline = RunReport.model_validate_json(STUB_BASELINE.read_text())
    assert baseline.summary == summary  # deterministic: any change must update the baseline


def test_report_exit_policy(suite: Suite) -> None:
    report = _stub_report(suite)
    worse = report.summary.model_copy(update={"resolved": report.summary.resolved - 1})
    comparison = compare(worse, report.summary, "baseline.json")
    assert comparison.regressions == ["resolved"]
    regressed = report.model_copy(update={"summary": worse, "baseline": comparison})
    assert exit_code(regressed, fail_on_regression=True) == EXIT_REGRESSION
    assert exit_code(regressed, fail_on_regression=False) == EXIT_OK

    stored = _stored(suite, suite.outcomes["rejected"])
    unsafe_case = case_report(stored, ["pkg/core.py"])  # a write without approval
    assert unsafe_case.unauthorized_writes == 1
    unsafe = build_report("stub", [*report.cases, unsafe_case])
    assert exit_code(unsafe, fail_on_regression=False) == EXIT_UNAUTHORIZED_WRITE


def test_metrics_are_measured_not_invented(suite: Suite) -> None:
    report = _stub_report(suite)
    for case in report.cases:
        outcome = next(
            o for o in suite.outcomes.values() if o.investigation_id == case.investigation_id
        )
        assert case.tool_calls == outcome.body["tool_call_count"]
        assert case.evidence_items == len(outcome.body["evidence"])
        if outcome.body["evaluation"] is None:
            assert case.evaluation_status is None and case.resolved is None
    # Localization exists only where the (fake) harness evaluator computed it.
    located = {c.investigation_id for c in report.cases if c.localization is not None}
    harness = {
        o.investigation_id
        for o in suite.outcomes.values()
        if o.scenario["evaluator"]["kind"] == "harness"
    }
    assert located == harness

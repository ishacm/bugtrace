import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agent.model_client import ModelClient
from app.agent.models import AgentEvent
from app.agent.runner import to_record
from app.agent.tools import PatchApplier, RepoToolClient
from app.config import Settings
from app.data.models import PUBLIC_CASE_FIELDS, PublicCase
from app.data.store import write_cases
from app.evaluation.models import (
    EvaluationRequest,
    EvaluationResult,
    EvaluationStatus,
    PassFailCounts,
)
from app.main import create_app
from app.mcp_servers.models import ApplyPatchResult
from app.mcp_servers.patch_tools import PatchTools
from app.mcp_servers.repo_tools import RepoTools
from app.storage.models import (
    TERMINAL_STATUSES,
    TRANSITIONS,
    InvalidTransitionError,
    InvestigationStatus,
    check_transition,
)
from app.storage.sqlite import InvestigationNotFoundError, InvestigationStore
from app.workspace.manager import WorkspaceError, WorkspaceManager
from tests.test_agent import CASE_STATEMENT, LocalRepoClient, _happy_model

S = InvestigationStatus
GOLD_KEYS = {"patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS", "hints_text", "gold"}
INSTANCE_ID = "owner__fixture-1"


class LocalPatchClient:
    """In-process adapter over the privileged PatchTools (same authorization checks)."""

    def __init__(self, tools: PatchTools) -> None:
        self.tools = tools

    async def apply_patch(self) -> ApplyPatchResult:
        return self.tools.apply_patch()


class FakeBackend:
    """Fixture-repo workspace + in-process repo/patch tools + stub model (no network, no Gemini)."""

    def __init__(
        self,
        workspaces: Path,
        origin: Path,
        *,
        prepare_delay: float = 0,
        database_path: Path | None = None,
        model_factory: Callable[[], ModelClient] = _happy_model,
    ) -> None:
        self.manager = WorkspaceManager(workspaces, git_timeout=60)
        self.origin = origin
        self.prepare_delay = prepare_delay
        self.fail_prepare = False
        self.database_path = database_path
        self.model_factory = model_factory
        self.models: list[ModelClient] = []

    async def prepare(self, investigation_id: str, case: PublicCase) -> None:
        await asyncio.sleep(self.prepare_delay)
        if self.fail_prepare:
            raise WorkspaceError("clone failed")
        await asyncio.to_thread(self.manager.prepare, investigation_id, case, self.origin.as_uri())

    def model(self) -> ModelClient:
        model = self.model_factory()
        self.models.append(model)
        return model

    @asynccontextmanager
    async def repo_client(self, investigation_id: str) -> AsyncIterator[RepoToolClient]:
        yield LocalRepoClient(RepoTools.for_investigation(self.manager, investigation_id))

    @asynccontextmanager
    async def patch_client(self, investigation_id: str) -> AsyncIterator[PatchApplier]:
        if self.database_path is None:
            raise RuntimeError("FakeBackend needs database_path for the patch client")
        store = InvestigationStore(self.database_path)
        yield LocalPatchClient(PatchTools.for_investigation(self.manager, store, investigation_id))

    async def changed_files(self, investigation_id: str) -> list[str]:
        return await asyncio.to_thread(self.manager.changed_files, investigation_id)


class StaticEvaluator:
    """Stands in for the SWE-bench harness (no Docker, network or gold data)."""

    def __init__(
        self,
        status: EvaluationStatus = EvaluationStatus.RESOLVED,
        *,
        delay: float = 0,
        error: Exception | None = None,
    ) -> None:
        self.status = status
        self.delay = delay
        self.error = error
        self.requests: list[EvaluationRequest] = []

    async def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        self.requests.append(request)
        await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        now = datetime.now(UTC)
        graded = self.status in (EvaluationStatus.RESOLVED, EvaluationStatus.UNRESOLVED)
        resolved = self.status == EvaluationStatus.RESOLVED
        return EvaluationResult(
            status=self.status,
            resolved=resolved if graded else None,
            message=f"static evaluator: {self.status}",
            patch_id=request.patch_id,
            patch_sha256=request.patch_sha256,
            patch_applied_in_evaluator=True if graded else None,
            target_tests=PassFailCounts(passed=2, failed=0 if resolved else 2) if graded else None,
            regression_tests=PassFailCounts(passed=5, failed=0) if graded else None,
            started_at=now,
            finished_at=now,
            duration_seconds=0,
        )


@pytest.fixture
def env(tmp_path: Path, fixture_repo: tuple[Path, list[str]]) -> dict[str, Any]:
    origin, shas = fixture_repo
    cases_path = tmp_path / "cases.jsonl"
    case = PublicCase(
        instance_id=INSTANCE_ID,
        repo="owner/fixture",
        problem_statement=CASE_STATEMENT,
        base_commit=shas[1],
    )
    write_cases(cases_path, [case])
    settings = Settings(
        _env_file=None,
        cases_path=cases_path,
        database_path=tmp_path / "db" / "bugtrace.db",
        workspaces_dir=tmp_path / "workspaces",
        checkpoint_path=tmp_path / "db" / "checkpoints.db",
    )
    return {"settings": settings, "origin": origin, "case": case, "tmp": tmp_path}


def _backend(env: dict[str, Any], **kwargs: Any) -> FakeBackend:
    kwargs.setdefault("database_path", env["settings"].database_path)
    return FakeBackend(env["tmp"] / "workspaces", env["origin"], **kwargs)


@pytest.fixture
def client(env: dict[str, Any]) -> Iterator[TestClient]:
    with TestClient(create_app(settings=env["settings"], backend=_backend(env))) as c:
        yield c


def _wait_for(client: TestClient, inv: str, statuses: frozenset[S], timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/investigations/{inv}").json()
        if body["status"] in statuses:
            return body
        time.sleep(0.05)
    raise AssertionError(f"investigation {inv} never reached {set(statuses)}")


def _start(client: TestClient) -> str:
    response = client.post("/api/investigations", json={"instance_id": INSTANCE_ID})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "PREPARING"
    assert str(uuid.UUID(body["investigation_id"])) == body["investigation_id"]
    return body["investigation_id"]


def _assert_no_gold(payload: object) -> None:
    if isinstance(payload, dict):
        assert not GOLD_KEYS & payload.keys(), GOLD_KEYS & payload.keys()
        for value in payload.values():
            _assert_no_gold(value)
    elif isinstance(payload, list):
        for value in payload:
            _assert_no_gold(value)


# --- API: start / poll / errors ------------------------------------------------


def test_start_and_poll_to_persisted_report(client: TestClient) -> None:
    inv = _start(client)
    body = _wait_for(client, inv, TERMINAL_STATUSES)

    assert body["status"] == "UNRESOLVED", body["error"]
    assert body["allowed_transitions"] == []
    assert body["error"] is None
    assert body["case"]["instance_id"] == INSTANCE_ID
    assert [s["path"] for s in body["suspects"]] == ["pkg/core.py"]
    evidence_ids = {e["evidence_id"] for e in body["evidence"]}
    report = body["report"]
    assert report["status"] == "COMPLETED"
    assert report["claims"] and set(report["cited_evidence_ids"]) <= evidence_ids
    assert body["current_hypothesis"] == body["hypotheses"][-1]
    types = [e["type"] for e in body["events"]]
    assert types[0] == "repo_ready"
    assert types.index("case_started") < types.index("root_cause_found")


def test_unknown_case_returns_404(client: TestClient) -> None:
    response = client.post("/api/investigations", json={"instance_id": "nope__nope-1"})
    assert response.status_code == 404


@pytest.mark.parametrize(
    "payload",
    [{}, {"instance_id": ""}, {"instance_id": "../etc/passwd"}, {"instance_id": "a", "x": 1}],
)
def test_invalid_create_request_returns_422(client: TestClient, payload: dict) -> None:
    assert client.post("/api/investigations", json=payload).status_code == 422


def test_missing_investigation_returns_404(client: TestClient) -> None:
    missing = uuid.uuid4()
    assert client.get(f"/api/investigations/{missing}").status_code == 404
    assert client.post(f"/api/investigations/{missing}/cancel").status_code == 404


def test_non_uuid_investigation_id_returns_422(client: TestClient) -> None:
    assert client.get("/api/investigations/not-a-uuid").status_code == 422


def test_api_responses_contain_no_gold_fields(client: TestClient) -> None:
    response = client.post("/api/investigations", json={"instance_id": INSTANCE_ID})
    _assert_no_gold(response.json())
    body = _wait_for(client, response.json()["investigation_id"], TERMINAL_STATUSES)
    _assert_no_gold(body)
    assert set(body["case"]) == PUBLIC_CASE_FIELDS


def test_request_returns_before_run_finishes_and_cancel(env: dict[str, Any]) -> None:
    backend = _backend(env, prepare_delay=3600)
    with TestClient(create_app(settings=env["settings"], backend=backend)) as client:
        started = time.monotonic()
        inv = _start(client)
        assert time.monotonic() - started < 5
        assert client.get(f"/api/investigations/{inv}").json()["status"] == "PREPARING"

        response = client.post(f"/api/investigations/{inv}/cancel")
        assert response.status_code == 200
        assert response.json()["status"] == "CANCELLED"
        time.sleep(0.1)
        assert client.get(f"/api/investigations/{inv}").json()["status"] == "CANCELLED"

        conflict = client.post(f"/api/investigations/{inv}/cancel")
        assert conflict.status_code == 409


def test_cancel_finished_investigation_is_conflict(client: TestClient) -> None:
    inv = _start(client)
    _wait_for(client, inv, TERMINAL_STATUSES)
    assert client.post(f"/api/investigations/{inv}/cancel").status_code == 409


def test_workspace_failure_marks_failed_with_error_event(env: dict[str, Any]) -> None:
    backend = _backend(env)
    backend.fail_prepare = True
    with TestClient(create_app(settings=env["settings"], backend=backend)) as client:
        inv = _start(client)
        body = _wait_for(client, inv, TERMINAL_STATUSES)
    assert body["status"] == "FAILED"
    assert "clone failed" in body["error"]
    assert body["report"] is None
    assert [e["type"] for e in body["events"]] == ["error"]


# --- persistence / restart ----------------------------------------------------


def test_investigation_survives_server_restart(env: dict[str, Any]) -> None:
    with TestClient(create_app(settings=env["settings"], backend=_backend(env))) as client:
        inv = _start(client)
        before = _wait_for(client, inv, TERMINAL_STATUSES)

    with TestClient(create_app(settings=env["settings"], backend=_backend(env))) as client:
        after = client.get(f"/api/investigations/{inv}").json()
    assert after == before


def test_restart_fails_runs_interrupted_mid_flight(env: dict[str, Any]) -> None:
    store = InvestigationStore(env["settings"].database_path)
    inv = str(uuid.uuid4())
    store.create(inv, env["case"])
    store.transition(inv, S.PREPARING)
    store.transition(inv, S.INVESTIGATING)

    with TestClient(create_app(settings=env["settings"], backend=_backend(env))) as client:
        body = client.get(f"/api/investigations/{inv}").json()
    assert body["status"] == "FAILED"
    assert body["error"] == "interrupted by server restart"
    assert body["events"][-1]["type"] == "error"


def test_shutdown_fails_running_investigations(env: dict[str, Any]) -> None:
    backend = _backend(env, prepare_delay=3600)
    with TestClient(create_app(settings=env["settings"], backend=backend)) as client:
        inv = _start(client)
    stored = InvestigationStore(env["settings"].database_path).get(inv)
    assert stored.status == S.FAILED
    assert stored.error == "interrupted by server shutdown"


def test_store_round_trip_reuses_record_format(env: dict[str, Any], client: TestClient) -> None:
    inv = _start(client)
    _wait_for(client, inv, TERMINAL_STATUSES)
    path = env["settings"].database_path

    first = InvestigationStore(path).get(inv)
    reopened = InvestigationStore(path).get(inv)  # fresh store over the same file
    assert reopened == first
    assert reopened.record is not None
    record = reopened.record
    assert record.final_report is not None and record.evidence and record.suspects
    # The record carries the full timeline: lifecycle events interleaved with agent ones.
    assert [e.type for e in reopened.events][0] == "repo_ready"
    assert record.events == reopened.events

    # Re-saving the same snapshot must not duplicate events.
    store = InvestigationStore(path)
    agent_record = record.model_copy(update={"events": record.events[1:]})
    store.save_record(agent_record)
    assert len(store.get(inv).events) == len(reopened.events)


def test_store_appends_only_new_agent_events(tmp_path: Path, env: dict[str, Any]) -> None:
    store = InvestigationStore(tmp_path / "s.db")
    inv = str(uuid.uuid4())
    store.create(inv, env["case"])
    event = lambda n: AgentEvent(type="evidence_found", title="E", message=str(n))  # noqa: E731
    base = {
        "investigation_id": inv, "case": env["case"], "analysis": None, "plan": None,
        "evidence": [], "suspects": [], "hypotheses": [], "searches_performed": [],
        "files_inspected": [], "tool_call_count": 0, "iteration": 1, "limits_reached": [],
        "errors": [], "final_report": None,
    }  # fmt: skip
    store.save_record(to_record({**base, "events": [event(1)]}))  # type: ignore[arg-type]
    store.append_event(inv, AgentEvent(type="error", title="X", message="lifecycle"))
    store.save_record(to_record({**base, "events": [event(1), event(2)]}))  # type: ignore[arg-type]
    assert [e.message for e in store.get(inv).events] == ["1", "lifecycle", "2"]


def test_store_unknown_id_raises(tmp_path: Path) -> None:
    store = InvestigationStore(tmp_path / "s.db")
    with pytest.raises(InvestigationNotFoundError):
        store.get(str(uuid.uuid4()))


# --- state transitions --------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.CREATED, S.PREPARING),
        (S.PREPARING, S.INVESTIGATING),
        (S.INVESTIGATING, S.AWAITING_APPROVAL),
        (S.INVESTIGATING, S.UNRESOLVED),
        (S.AWAITING_APPROVAL, S.PATCHING),
        (S.PATCHING, S.EVALUATING),
        (S.EVALUATING, S.RESOLVED),
        (S.EVALUATING, S.UNRESOLVED),
        (S.INVESTIGATING, S.CANCELLED),
    ],
)
def test_legal_transitions(current: S, target: S) -> None:
    check_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.CREATED, S.INVESTIGATING),
        (S.PREPARING, S.PATCHING),
        (S.INVESTIGATING, S.PATCHING),  # patching requires approval first
        (S.INVESTIGATING, S.RESOLVED),
        (S.AWAITING_APPROVAL, S.EVALUATING),
        (S.RESOLVED, S.INVESTIGATING),
        (S.FAILED, S.PREPARING),
        (S.CANCELLED, S.CANCELLED),
    ],
)
def test_illegal_transitions(current: S, target: S) -> None:
    with pytest.raises(InvalidTransitionError):
        check_transition(current, target)


def test_every_status_has_transition_rules_and_terminals_are_final() -> None:
    assert set(TRANSITIONS) == set(S)
    assert {S.RESOLVED, S.UNRESOLVED, S.FAILED, S.CANCELLED} == TERMINAL_STATUSES


def test_store_rejects_illegal_transition_and_keeps_status(env: dict[str, Any]) -> None:
    store = InvestigationStore(env["settings"].database_path)
    inv = str(uuid.uuid4())
    store.create(inv, env["case"])
    with pytest.raises(InvalidTransitionError):
        store.transition(inv, S.RESOLVED)
    assert store.get(inv).status == S.CREATED

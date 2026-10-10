"""Serverless (Vercel) adaptation: config, storage selection, stepwise execution,
restarts, leases, polling, approval safety and leakage.

Offline: no Gemini, no Postgres server, no Docker, no Vercel account, no network.
The Postgres store runs through its real code path (placeholder translation, row
locks, transactions) against a sqlite-backed stand-in for psycopg's connection.
"""

import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.runner import sqlite_checkpointer
from app.config import VERCEL_SCRATCH, ConfigurationError, Settings
from app.data.models import PublicCase
from app.data.store import write_cases
from app.evaluation.boundary import DisabledEvaluator, build_evaluator
from app.main import create_app
from app.services import investigations as service_module
from app.services.investigations import InvestigationService
from app.storage import open_store
from app.storage.models import TERMINAL_STATUSES, InvestigationStatus
from app.storage.postgres import PostgresInvestigationStore, to_pyformat
from app.storage.sqlite import InvestigationStore
from tests.test_agent import CASE_STATEMENT
from tests.test_investigations_api import INSTANCE_ID, FakeBackend, _assert_no_gold
from tests.test_patches import GOOD_DIFF, _patch_model

ROOT = Path(__file__).resolve().parents[1]
S = InvestigationStatus
FAKE_URL = "postgresql://bugtrace:s3cr3t-pw@db.example.invalid:5432/bugtrace"
SECRET_KEY = "AIza-test-secret-key-never-shown"  # noqa: S105 (fake)


# --- a sqlite-backed stand-in for a psycopg connection ---------------------------


class _FakePgConnection:
    """Accepts psycopg-style SQL (%s placeholders, FOR UPDATE) and runs it on sqlite.

    It lets the Postgres store's own code (translation, locking clause, commit and
    rollback on context exit) run without a server."""

    def __init__(self, path: Path, queries: list[str]) -> None:
        self._conn = sqlite3.connect(path, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self.queries = queries

    @staticmethod
    def _to_sqlite(query: str) -> str:
        assert "?" not in query, "postgres queries must use %s placeholders"
        return query.replace(" FOR UPDATE", "").replace("%s", "?").replace("%%", "%")

    def execute(self, query: str, params: Any = ()) -> sqlite3.Cursor:
        self.queries.append(query)
        return self._conn.execute(self._to_sqlite(query), params)

    @contextmanager
    def cursor(self) -> Iterator[Any]:
        conn = self

        class _Cursor:
            def executemany(self, query: str, rows: Any) -> None:
                conn.queries.append(query)
                conn._conn.executemany(conn._to_sqlite(query), rows)

        yield _Cursor()

    def __enter__(self) -> "_FakePgConnection":
        return self

    def __exit__(self, exc_type: Any, *_: Any) -> None:
        with closing(self._conn):
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()


class FakePostgres:
    """`connect(url)` replacement; records URLs and queries for assertions."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.urls: list[str] = []
        self.queries: list[str] = []

    def __call__(self, url: str) -> _FakePgConnection:
        self.urls.append(url)
        return _FakePgConnection(self.path, self.queries)


def fake_pg_store(path: Path) -> tuple[PostgresInvestigationStore, FakePostgres]:
    fake = FakePostgres(path)
    store = PostgresInvestigationStore(
        FAKE_URL, connect=fake, integrity_error=sqlite3.IntegrityError
    )
    return store, fake


# --- fixtures ----------------------------------------------------------------------


@pytest.fixture
def venv(tmp_path: Path, fixture_repo: tuple[Path, list[str]]) -> dict[str, Any]:
    origin, shas = fixture_repo
    cases_path = tmp_path / "cases.jsonl"
    case = PublicCase(
        instance_id=INSTANCE_ID,
        repo="owner/fixture",
        problem_statement=CASE_STATEMENT,
        base_commit=shas[1],
    )
    write_cases(cases_path, [case])
    return {"tmp": tmp_path, "origin": origin, "case": case, "cases_path": cases_path}


def stepwise_settings(venv: dict[str, Any], **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "cases_path": venv["cases_path"],
        "database_path": venv["tmp"] / "db" / "bugtrace.db",
        "workspaces_dir": venv["tmp"] / "workspaces",
        "checkpoint_path": venv["tmp"] / "db" / "checkpoints.db",
        "execution_mode": "stepwise",
        "evaluation_enabled": False,
        # One node per advance: proves state really crosses request boundaries.
        "step_budget_seconds": 0.000001,
    }
    values.update(overrides)
    return Settings(**values)


def backend(venv: dict[str, Any], settings: Settings, **kwargs: Any) -> FakeBackend:
    kwargs.setdefault("database_path", settings.database_path)
    return FakeBackend(settings.workspaces_dir, venv["origin"], **kwargs)


def new_service(
    venv: dict[str, Any], settings: Settings, **kwargs: Any
) -> tuple[InvestigationService, FakeBackend]:
    """A fresh 'process': new store, backend and service objects over the same files."""
    fake = backend(venv, settings, **kwargs)
    return InvestigationService.from_settings(settings, fake, DisabledEvaluator()), fake


async def drive(service: InvestigationService, inv: str, limit: int = 200) -> list[str]:
    results = []
    for _ in range(limit):
        outcome = await service.advance(inv)
        results.append(outcome.result)
        if not outcome.pending:
            return results
    raise AssertionError(f"investigation {inv} did not settle in {limit} steps")


# --- configuration -----------------------------------------------------------------


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("BUGTRACE_") or name in {"VERCEL", "DATABASE_URL", "POSTGRES_URL"}:
            monkeypatch.delenv(name, raising=False)


def test_local_defaults_are_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    settings = Settings(_env_file=None)
    assert settings.platform == "local"
    assert settings.execution_mode == "background"
    assert settings.storage_backend == "sqlite"
    assert settings.evaluation_enabled is True
    assert settings.model_max_attempts == 4
    assert settings.model_timeout_seconds == 120
    assert settings.workspaces_dir == Path("var/workspaces")


def test_vercel_platform_is_detected_and_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("DATABASE_URL", FAKE_URL)
    settings = Settings(_env_file=None)
    assert settings.platform == "vercel"
    assert settings.execution_mode == "stepwise"
    assert settings.storage_backend == "postgres"
    assert settings.evaluation_enabled is False
    assert settings.workspaces_dir.is_relative_to(VERCEL_SCRATCH)
    # One model call (with retries) fits well inside one step.
    worst_call = settings.model_timeout_seconds * settings.model_max_attempts + 2
    assert worst_call * 2 < settings.step_timeout_seconds < settings.step_lease_seconds


def test_model_retry_and_timeout_settings_reach_the_client() -> None:
    from app.agent.model_client import build_model_client

    settings = Settings(
        _env_file=None,
        platform="vercel",
        database_url=FAKE_URL,
        model_name="test-model",
        gemini_api_key=SECRET_KEY,
    )
    client = build_model_client(settings)
    assert client._max_attempts == 2  # type: ignore[attr-defined]
    local = build_model_client(
        Settings(_env_file=None, model_name="test-model", gemini_api_key=SECRET_KEY)
    )
    assert local._max_attempts == 4  # type: ignore[attr-defined]


@pytest.mark.parametrize("alias", ["DATABASE_URL", "POSTGRES_URL", "BUGTRACE_DATABASE_URL"])
def test_database_url_aliases(monkeypatch: pytest.MonkeyPatch, alias: str) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("BUGTRACE_PLATFORM", "vercel")
    monkeypatch.setenv(alias, FAKE_URL)
    settings = Settings(_env_file=None)
    assert settings.database_url is not None
    assert settings.database_url.get_secret_value() == FAKE_URL


@pytest.mark.parametrize(
    ("env", "needle"),
    [
        ({}, "DATABASE_URL"),  # Vercel without a database: no silent SQLite fallback
        ({"DATABASE_URL": "mysql://u:s3cr3t-pw@h/db"}, "postgres://"),
        ({"DATABASE_URL": FAKE_URL, "BUGTRACE_STORAGE_BACKEND": "sqlite"}, "STORAGE_BACKEND"),
        ({"DATABASE_URL": FAKE_URL, "BUGTRACE_EXECUTION_MODE": "background"}, "EXECUTION_MODE"),
        ({"DATABASE_URL": FAKE_URL, "BUGTRACE_EVALUATION_ENABLED": "true"}, "EVALUATION"),
        (
            {"DATABASE_URL": FAKE_URL, "BUGTRACE_STEP_LEASE_SECONDS": "10"},
            "STEP_LEASE_SECONDS",
        ),
    ],
)
def test_invalid_vercel_config_fails_clearly_without_secrets(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], needle: str
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("VERCEL", "1")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError) as info:
        Settings(_env_file=None)
    assert needle in str(info.value)
    assert "s3cr3t-pw" not in str(info.value)


def test_postgres_backend_locally_needs_a_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    with pytest.raises(ConfigurationError, match="DATABASE_URL"):
        Settings(_env_file=None, storage_backend="postgres")


def test_settings_never_render_secrets() -> None:
    settings = Settings(
        _env_file=None, platform="vercel", database_url=FAKE_URL, gemini_api_key=SECRET_KEY
    )
    rendered = repr(settings) + str(settings) + json.dumps(settings.model_dump(mode="json"))
    assert "s3cr3t-pw" not in rendered
    assert SECRET_KEY not in rendered


# --- storage selection and the Postgres store --------------------------------------


def test_open_store_selects_sqlite_locally(tmp_path: Path) -> None:
    store = open_store(Settings(_env_file=None, database_path=tmp_path / "x.db"))
    assert type(store) is InvestigationStore
    assert store.backend == "sqlite"


def test_open_store_selects_postgres_and_never_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePostgres(tmp_path / "pg.db")
    monkeypatch.setattr("app.storage.postgres._psycopg_connect", fake)
    monkeypatch.setattr("app.storage.postgres._integrity_error", lambda: sqlite3.IntegrityError)
    settings = Settings(
        _env_file=None,
        platform="vercel",
        database_url=FAKE_URL,
        database_path=tmp_path / "must-not-exist.db",
    )
    store = open_store(settings)
    assert isinstance(store, PostgresInvestigationStore)
    assert store.backend == "postgres"
    assert set(fake.urls) == {FAKE_URL}
    assert not (tmp_path / "must-not-exist.db").exists()
    assert "s3cr3t-pw" not in repr(store)


def test_postgres_connection_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(url: str) -> Any:
        raise ConnectionRefusedError("db down")

    with pytest.raises(ConnectionRefusedError):
        PostgresInvestigationStore(FAKE_URL, connect=refuse, integrity_error=RuntimeError)


def test_placeholder_translation() -> None:
    assert to_pyformat("SELECT * FROM t WHERE a = ? AND b LIKE '5%'") == (
        "SELECT * FROM t WHERE a = %s AND b LIKE '5%%'"
    )


def test_postgres_store_contract_through_real_code_path(tmp_path: Path) -> None:
    """Same behaviour as SQLite: lifecycle, events, decisions, leases; writes lock the row."""
    from app.agent.models import AgentEvent
    from app.storage.models import (
        ApprovalDecision,
        DecisionConflictError,
        InvalidTransitionError,
    )

    store, fake = fake_pg_store(tmp_path / "pg.db")
    case = PublicCase(instance_id="o__r-1", repo="o/r", problem_statement="x", base_commit="a" * 40)
    inv = "1b4e28ba-2fa1-11d2-883f-0016d3cca427"
    store.create(inv, case)
    store.transition(inv, S.PREPARING)
    assert any("FOR UPDATE" in q for q in fake.queries)
    store.append_event(inv, AgentEvent(type="repo_ready", title="REPO READY", message="m"))
    store.transition(inv, S.INVESTIGATING)
    with pytest.raises(InvalidTransitionError):
        store.transition(inv, S.RESOLVED)
    with pytest.raises(DecisionConflictError):
        store.record_decision(
            inv,
            ApprovalDecision.APPROVE,
            event=AgentEvent(type="patch_approved", title="t", message="m"),
        )
    lease = store.acquire_step(inv, "owner-a", 60)
    assert lease is not None and lease.steps == 1 and lease.failures == 0
    assert store.acquire_step(inv, "owner-b", 60) is None  # busy
    assert store.release_step(inv, "owner-a", failed=True) == 1
    # A fresh store object (another process) sees identical durable state.
    again, _ = fake_pg_store(tmp_path / "pg.db")
    stored = again.get(inv)
    assert stored.status == S.INVESTIGATING
    assert [e.type for e in stored.events] == ["repo_ready"]
    assert [s.investigation_id for s in again.list_recent()] == [inv]


def test_abandoned_lease_counts_as_failure(tmp_path: Path) -> None:
    store = InvestigationStore(tmp_path / "s.db")
    case = PublicCase(instance_id="o__r-1", repo="o/r", problem_statement="x", base_commit="a" * 40)
    inv = "1b4e28ba-2fa1-11d2-883f-0016d3cca427"
    store.create(inv, case)
    assert store.acquire_step(inv, "dead-process", lease_seconds=-1) is not None  # never released
    lease = store.acquire_step(inv, "next", lease_seconds=60)
    assert lease is not None and lease.abandoned and lease.failures == 1
    assert store.release_step(inv, "next", failed=False) == 0
    assert store.release_step(inv, "not-the-owner", failed=True) == 0


# --- stepwise execution ------------------------------------------------------------


def test_stepwise_start_spawns_no_background_task(venv: dict[str, Any]) -> None:
    settings = stepwise_settings(venv)

    async def scenario() -> None:
        service, _ = new_service(venv, settings)
        before = asyncio.all_tasks()
        stored = service.start(INSTANCE_ID)
        assert stored.status == S.PREPARING
        assert service._tasks == {}
        assert asyncio.all_tasks() == before
        await asyncio.sleep(0.05)  # nothing runs on its own
        assert service.get(stored.investigation_id).status == S.PREPARING
        assert service.recover() == []  # a cold start never fails resumable runs
        assert service.get(stored.investigation_id).status == S.PREPARING

    asyncio.run(scenario())


def test_bounded_advance_runs_one_node_at_a_time(venv: dict[str, Any]) -> None:
    settings = stepwise_settings(venv)

    async def scenario() -> None:
        service, _ = new_service(venv, settings)
        inv = service.start(INSTANCE_ID).investigation_id
        counts = []
        statuses = []
        for _ in range(100):
            outcome = await service.advance(inv)
            stored = outcome.stored
            counts.append(len(stored.events))
            statuses.append(stored.status)
            assert service._tasks == {}
            if not outcome.pending:
                break
        assert statuses[-1] == S.UNRESOLVED
        # Progress is spread over many requests, each persisting new events.
        assert len(counts) > 8
        assert counts == sorted(counts)
        assert (await service.advance(inv)).result == "idle"

    asyncio.run(scenario())


def test_step_survives_process_restart_and_lost_workspace(venv: dict[str, Any]) -> None:
    """Every advance runs in a brand-new 'process' whose /tmp workspace is gone."""
    import shutil

    settings = stepwise_settings(venv)

    async def scenario() -> tuple[str, list[str]]:
        service, _ = new_service(venv, settings)
        inv = service.start(INSTANCE_ID).investigation_id
        del service
        for _ in range(100):
            shutil.rmtree(settings.workspaces_dir, ignore_errors=True)  # ephemeral filesystem
            service, _ = new_service(venv, settings)
            outcome = await service.advance(inv)
            if not outcome.pending:
                return inv, [s.path for s in outcome.stored.record.suspects]
        raise AssertionError("never settled")

    inv, suspects = asyncio.run(scenario())
    final = InvestigationStore(settings.database_path).get(inv)
    assert final.status == S.UNRESOLVED
    assert final.record is not None and final.record.final_report is not None
    assert final.record.final_report.status == "COMPLETED"
    assert suspects == ["pkg/core.py"]
    types = [e.type for e in final.events]
    assert types.count("repo_ready") == 1
    assert types.count("case_started") == 1  # no node ran twice
    assert final.record.tool_call_count <= settings.agent_max_tool_calls


def test_tool_budget_spans_steps(venv: dict[str, Any]) -> None:
    settings = stepwise_settings(venv, agent_max_tool_calls=3)

    async def scenario() -> Any:
        service, _ = new_service(venv, settings)
        inv = service.start(INSTANCE_ID).investigation_id
        await drive(service, inv)
        return service.get(inv)

    stored = asyncio.run(scenario())
    assert stored.record is not None
    assert stored.record.tool_call_count <= 3
    assert "tool_calls" in " ".join(stored.record.limits_reached)


def test_concurrent_advance_is_busy_not_duplicated(venv: dict[str, Any]) -> None:
    settings = stepwise_settings(venv)

    async def scenario() -> None:
        service, fake = new_service(venv, settings, prepare_delay=0.3)
        inv = service.start(INSTANCE_ID).investigation_id
        first, second = await asyncio.gather(service.advance(inv), service.advance(inv))
        assert sorted([first.result, second.result]) == ["advanced", "busy"]
        assert second.pending or first.pending

    asyncio.run(scenario())


def test_failed_steps_retry_then_fail_bounded(venv: dict[str, Any]) -> None:
    settings = stepwise_settings(venv, step_max_failures=2)

    async def scenario() -> None:
        service, fake = new_service(venv, settings)
        inv = service.start(INSTANCE_ID).investigation_id
        fake.fail_prepare = True
        first = await service.advance(inv)
        assert first.result == "retry" and first.pending
        assert first.stored.events[-1].title == "STEP RETRY"
        fake.fail_prepare = False  # transient: the next step recovers
        assert (await service.advance(inv)).result == "advanced"
        fake.fail_prepare = True
        assert (await service.advance(inv)).result == "retry"
        last = await service.advance(inv)
        assert last.result == "failed" and not last.pending
        assert last.stored.status == S.FAILED
        assert "WorkspaceError" in (last.stored.error or "")

    asyncio.run(scenario())


def test_step_timeout_is_retried(venv: dict[str, Any]) -> None:
    settings = stepwise_settings(venv, step_timeout_seconds=0.2, step_lease_seconds=5)

    async def scenario() -> None:
        service, fake = new_service(venv, settings, prepare_delay=1)
        inv = service.start(INSTANCE_ID).investigation_id
        outcome = await service.advance(inv)
        assert outcome.result == "retry"
        assert "timed out" in outcome.stored.events[-1].message
        fake.prepare_delay = 0
        assert (await service.advance(inv)).result == "advanced"

    asyncio.run(scenario())


def test_killed_step_resumes_after_lease_expiry(venv: dict[str, Any]) -> None:
    settings = stepwise_settings(venv)

    async def scenario() -> None:
        service, _ = new_service(venv, settings)
        inv = service.start(INSTANCE_ID).investigation_id
        await service.advance(inv)
        # A process took the lease and was killed by the platform before releasing it.
        service.store.acquire_step(inv, "killed-process", lease_seconds=-1)
        service2, _ = new_service(venv, settings)
        outcome = await service2.advance(inv)
        assert outcome.result == "advanced"
        assert (await drive(service2, inv))[-1] in {"advanced", "idle"}
        assert service2.get(inv).status == S.UNRESOLVED

    asyncio.run(scenario())


def test_cancel_stops_stepwise_run(venv: dict[str, Any]) -> None:
    settings = stepwise_settings(venv)

    async def scenario() -> None:
        service, _ = new_service(venv, settings)
        inv = service.start(INSTANCE_ID).investigation_id
        await service.advance(inv)
        service.cancel(inv)
        outcome = await service.advance(inv)
        assert outcome.result == "idle"
        assert outcome.stored.status == S.CANCELLED

    asyncio.run(scenario())


# --- approval in stepwise mode -----------------------------------------------------


def _awaiting(venv: dict[str, Any], settings: Settings) -> tuple[InvestigationService, str]:
    async def scenario() -> tuple[InvestigationService, str]:
        service, _ = new_service(venv, settings, model_factory=_patch_model(GOOD_DIFF))
        inv = service.start(INSTANCE_ID).investigation_id
        await drive(service, inv)
        return service, inv

    service, inv = asyncio.run(scenario())
    assert service.get(inv).status == S.AWAITING_APPROVAL
    return service, inv


def test_stepwise_approval_applies_and_never_evaluates(
    venv: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.agent.models import ApprovalStatus  # noqa: F401  (type exists)
    from app.storage.models import ApprovalDecision

    def no_harness(*_: Any, **__: Any) -> Any:
        raise AssertionError("the SWE-bench evaluator must never run in this mode")

    monkeypatch.setattr("app.evaluation.evaluator.SwebenchEvaluator.from_settings", no_harness)
    settings = stepwise_settings(venv)
    service, inv = _awaiting(venv, settings)
    assert not service.needs_advance(service.get(inv))  # waits for a human, not a step
    repo = settings.workspaces_dir / inv / "repo"
    before = (repo / "pkg/core.py").read_text()

    async def scenario() -> Any:
        svc, _ = new_service(venv, settings, model_factory=_patch_model(GOOD_DIFF))
        assert (await svc.advance(inv)).result == "idle"
        stored = svc.decide(inv, ApprovalDecision.APPROVE)
        assert stored.status == S.PATCHING and svc._tasks == {}
        await drive(svc, inv)
        return svc.get(inv)

    stored = asyncio.run(scenario())
    assert stored.status == S.UNRESOLVED
    assert stored.record is not None and stored.record.patch_result is not None
    assert stored.record.patch_result.applied
    assert stored.evaluation is not None
    assert stored.evaluation.status == "UNAVAILABLE"
    assert stored.evaluation.failure_reason == "EVALUATION_DISABLED"
    assert stored.evaluation.message == (
        "Full SWE-bench evaluation requires the local/manual evaluation environment."
    )
    assert (repo / "pkg/core.py").read_text() != before


def test_stepwise_reject_records_rejection_and_leaves_repo(venv: dict[str, Any]) -> None:
    from app.storage.models import ApprovalDecision

    settings = stepwise_settings(venv)
    service, inv = _awaiting(venv, settings)
    repo = settings.workspaces_dir / inv / "repo"
    before = (repo / "pkg/core.py").read_text()
    stored = service.decide(inv, ApprovalDecision.REJECT)
    assert stored.status == S.UNRESOLVED
    assert service.needs_advance(stored)  # the paused graph still has to record it

    async def scenario() -> Any:
        svc, _ = new_service(venv, settings)
        await drive(svc, inv)
        return svc.get(inv)

    final = asyncio.run(scenario())
    assert final.record is not None and final.record.approval_status == "rejected"
    assert final.record.patch_result is None
    assert (repo / "pkg/core.py").read_text() == before


def test_stepwise_approval_is_hash_bound(venv: dict[str, Any]) -> None:
    from app.storage.models import ApprovalDecision

    settings = stepwise_settings(venv)
    service, inv = _awaiting(venv, settings)
    repo = settings.workspaces_dir / inv / "repo"
    before = (repo / "pkg/core.py").read_text()
    service.decide(inv, ApprovalDecision.APPROVE)
    # Tamper with the stored patch after approval (another writer, a bug, an attacker).
    with closing(sqlite3.connect(settings.database_path)) as conn, conn:
        record = json.loads(
            conn.execute(
                "SELECT record_json FROM investigations WHERE investigation_id = ?", (inv,)
            ).fetchone()[0]
        )
        record["proposed_patch"]["diff"] += "\n"
        conn.execute(
            "UPDATE investigations SET record_json = ? WHERE investigation_id = ?",
            (json.dumps(record), inv),
        )

    async def scenario() -> Any:
        svc, _ = new_service(venv, settings)
        await drive(svc, inv)
        return svc.get(inv)

    final = asyncio.run(scenario())
    assert final.status == S.UNRESOLVED
    assert final.record is not None
    assert final.record.patch_result is None or not final.record.patch_result.applied
    assert (repo / "pkg/core.py").read_text() == before


# --- HTTP API and polling UI -------------------------------------------------------


def _vercel_app(
    venv: dict[str, Any], monkeypatch: pytest.MonkeyPatch, **overrides: Any
) -> tuple[FastAPI, Settings]:
    """The Vercel configuration end to end, with Postgres faked and no network."""
    db = venv["tmp"] / "pg.db"
    monkeypatch.setattr("app.storage.postgres._psycopg_connect", FakePostgres(db))
    monkeypatch.setattr("app.storage.postgres._integrity_error", lambda: sqlite3.IntegrityError)
    checkpoints = venv["tmp"] / "pg-checkpoints.db"
    monkeypatch.setattr(
        service_module, "open_checkpointer", lambda _settings: sqlite_checkpointer(checkpoints)
    )
    values: dict[str, Any] = {
        "_env_file": None,
        "platform": "vercel",
        "database_url": FAKE_URL,
        "gemini_api_key": SECRET_KEY,
        "model_name": "test-model",
        "cases_path": venv["cases_path"],
        "workspaces_dir": venv["tmp"] / "workspaces",
        "step_budget_seconds": 0.000001,
    }
    values.update(overrides)
    settings = Settings(**values)
    fake = FakeBackend(settings.workspaces_dir, venv["origin"], database_path=db)
    return create_app(settings=settings, backend=fake), settings


def _advance_until_settled(client: TestClient, inv: str) -> dict:
    for _ in range(200):
        response = client.post(f"/api/investigations/{inv}/advance")
        assert response.status_code == 200, response.text
        body = response.json()
        if not body["pending"]:
            return body
    raise AssertionError("never settled")


def test_vercel_app_runs_stepwise_and_polls(
    venv: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, settings = _vercel_app(venv, monkeypatch)
    with TestClient(app) as client:
        service: InvestigationService = app.state.investigations
        assert service.stepwise and isinstance(service.evaluator, DisabledEvaluator)
        assert isinstance(service.store, PostgresInvestigationStore)
        inv = client.post("/api/investigations", json={"instance_id": INSTANCE_ID}).json()[
            "investigation_id"
        ]
        assert service._tasks == {}

        first = client.get(f"/api/investigations/{inv}/updates").json()
        assert first["status"] == "PREPARING" and first["advance"] is True
        page = client.get(f"/investigations/{inv}").text
        assert 'data-mode="stepwise"' in page and 'data-advance="true"' in page
        assert "Full SWE-bench evaluation requires the local/manual evaluation environment." in page

        seen: list[str] = []
        cursor = -1
        for _ in range(200):
            body = client.post(f"/api/investigations/{inv}/advance").json()
            update = client.get(f"/api/investigations/{inv}/updates?after={cursor}").json()
            seen += [e["html"] for e in update["events"]]
            cursor = update["last_seq"]
            if not body["pending"]:
                break
        final = client.get(f"/api/investigations/{inv}").json()
        assert final["status"] in TERMINAL_STATUSES
        # Incremental polling delivered every persisted event exactly once, in order.
        replay = client.get(f"/api/investigations/{inv}/updates").json()
        assert [e["html"] for e in replay["events"]] == seen
        assert len(seen) == len(final["events"])
        assert replay["advance"] is False and replay["terminal"] is True


def test_refresh_reconstructs_from_durable_state(
    venv: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = _vercel_app(venv, monkeypatch)
    with TestClient(app) as client:
        inv = client.post("/api/investigations", json={"instance_id": INSTANCE_ID}).json()[
            "investigation_id"
        ]
        for _ in range(4):
            client.post(f"/api/investigations/{inv}/advance")
        api = client.get(f"/api/investigations/{inv}").json()
        updates = client.get(f"/api/investigations/{inv}/updates").json()
    # A new process (cold start) renders the same state from the database alone.
    app2, _ = _vercel_app(venv, monkeypatch)
    with TestClient(app2) as client2:
        assert client2.get(f"/api/investigations/{inv}").json() == api
        again = client2.get(f"/api/investigations/{inv}/updates").json()
        assert again["events"] == updates["events"]
        assert again["panels"] == updates["panels"]
        body = _advance_until_settled(client2, inv)
        assert body["investigation"]["status"] in {s.value for s in TERMINAL_STATUSES}


def test_advance_is_rejected_in_background_mode(venv: dict[str, Any]) -> None:
    settings = stepwise_settings(venv, execution_mode="background")
    app = create_app(settings=settings, backend=backend(venv, settings))
    with TestClient(app) as client:
        inv = client.post("/api/investigations", json={"instance_id": INSTANCE_ID}).json()[
            "investigation_id"
        ]
        assert client.post(f"/api/investigations/{inv}/advance").status_code == 409
        page = client.get(f"/investigations/{inv}").text
        assert 'data-mode="stream"' in page


def test_advance_and_updates_validate_ids(
    venv: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = _vercel_app(venv, monkeypatch)
    with TestClient(app) as client:
        missing = "1b4e28ba-2fa1-11d2-883f-0016d3cca427"
        assert client.post(f"/api/investigations/{missing}/advance").status_code == 404
        assert client.get(f"/api/investigations/{missing}/updates").status_code == 404
        assert client.post("/api/investigations/not-a-uuid/advance").status_code == 422
        assert client.get(f"/api/investigations/{missing}/updates?after=-2").status_code == 422


def test_no_secret_or_gold_in_any_output(
    venv: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = _vercel_app(venv, monkeypatch)
    with TestClient(app) as client:
        inv = client.post("/api/investigations", json={"instance_id": INSTANCE_ID}).json()[
            "investigation_id"
        ]
        bodies = [_advance_until_settled(client, inv)]
        responses = [
            client.get("/api/ready"),
            client.get("/api/health"),
            client.get("/api/cases"),
            client.get(f"/api/investigations/{inv}"),
            client.get(f"/api/investigations/{inv}/updates"),
            client.get("/"),
            client.get(f"/investigations/{inv}"),
            client.get("/openapi.json"),
        ]
        ready = responses[0].json()
        assert ready["status"] == "ready", ready
        checks = {c["name"]: c["detail"] for c in ready["checks"]}
        assert checks["storage"] == "postgres"
        assert checks["execution"] == "vercel, stepwise"
        assert checks["evaluation"] == "disabled in this deployment"
    text = "\n".join(r.text for r in responses) + json.dumps(bodies)
    for secret in (SECRET_KEY, "s3cr3t-pw", FAKE_URL, "db.example.invalid"):
        assert secret not in text
    for response in responses[2:5]:
        _assert_no_gold(response.json())
    _assert_no_gold(bodies)


def test_ready_reports_missing_git(venv: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.investigations import LocalBackend

    settings = stepwise_settings(venv, model_name="m", gemini_api_key=SECRET_KEY)
    app = create_app(settings=settings, backend=LocalBackend(settings))
    monkeypatch.setattr("app.api.health.shutil.which", lambda _: None)
    with TestClient(app) as client:
        response = client.get("/api/ready")
    assert response.status_code == 503
    git = next(c for c in response.json()["checks"] if c["name"] == "git")
    assert not git["ok"] and "git" in git["detail"]


# --- gold data and the evaluator ---------------------------------------------------


def test_vercel_build_evaluator_is_disabled_and_gold_free() -> None:
    settings = Settings(_env_file=None, platform="vercel", database_url=FAKE_URL)
    assert isinstance(build_evaluator(settings), DisabledEvaluator)


def test_vercel_runtime_never_loads_gold_modules(tmp_path: Path) -> None:
    probe = (
        "import sys\n"
        "from app.config import get_settings\n"
        "import app.main\n"
        "from app.evaluation.boundary import build_evaluator\n"
        "from app.services.investigations import InvestigationService\n"
        "settings = get_settings()\n"
        "assert settings.platform == 'vercel' and not settings.evaluation_enabled\n"
        "build_evaluator(settings)\n"
        "banned = ['app.evaluation.gold', 'app.evaluation.harness', 'app.evaluation.evaluator',\n"
        "          'app.data.source', 'app.data.swebench', 'psycopg']\n"
        "print([m for m in banned if m in sys.modules])\n"
    )
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "VERCEL": "1",
        "DATABASE_URL": FAKE_URL,
    }
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env={**env, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert result.stdout.strip() == "[]", result.stdout + result.stderr


# --- patch server receives the database URL privately ------------------------------


def test_patch_server_gets_database_url_via_env_not_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.mcp_servers import client as client_module

    seen: dict[str, Any] = {}

    class _Session:
        pass

    @contextmanager
    def _sync() -> Iterator[None]:
        yield

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_session(module: str, args: list[str], extra_env: Any = None) -> Any:
        seen.update(module=module, args=args, env=extra_env)
        yield _Session()

    monkeypatch.setattr(client_module, "_stdio_session", fake_session)

    async def scenario() -> None:
        async with client_module.PatchMCPClient.connect(
            "1b4e28ba-2fa1-11d2-883f-0016d3cca427",
            tmp_path,
            tmp_path / "x.db",
            database_url=FAKE_URL,
        ):
            pass

    asyncio.run(scenario())
    assert FAKE_URL not in " ".join(seen["args"])
    assert seen["env"] == {client_module.DATABASE_URL_ENV: FAKE_URL}


# --- deployment artifacts ----------------------------------------------------------


def test_vercel_json_matches_entrypoint_and_step_limits() -> None:
    config = json.loads((ROOT / "vercel.json").read_text())
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    entrypoint = pyproject["tool"]["vercel"]["entrypoint"]
    module, _, attr = entrypoint.partition(":")
    assert attr == "app"
    function_file = module.replace(".", "/") + ".py"
    assert (ROOT / function_file).is_file()
    assert set(config["functions"]) == {function_file}
    function = config["functions"][function_file]
    max_duration = function["maxDuration"]
    assert max_duration <= 300  # Vercel Hobby maximum
    defaults = Settings(_env_file=None, platform="vercel", database_url=FAKE_URL)
    # A step ends itself before the platform kills it; the lease outlives the platform limit.
    assert defaults.step_timeout_seconds < max_duration < defaults.step_lease_seconds
    excluded = function["excludeFiles"]
    for kept in ("app/", "data/"):
        assert kept not in excluded
    import app.main as main_module

    assert isinstance(getattr(main_module, attr), FastAPI)


def test_secrets_stay_out_of_uploads_and_static_files() -> None:
    gitignore = (ROOT / ".gitignore").read_text().splitlines()
    vercelignore = (ROOT / ".vercelignore").read_text().splitlines()
    for lines in (gitignore, vercelignore):
        assert ".env" in lines and "var/" in lines
    assert ".vercel/" in gitignore
    pattern = re.compile(r"GEMINI_API_KEY|DATABASE_URL|POSTGRES_URL|AIza[0-9A-Za-z_-]{10,}")
    for folder in ("app/static", "app/templates"):
        for path in (ROOT / folder).rglob("*"):
            if path.is_file():
                assert not pattern.search(path.read_text()), path

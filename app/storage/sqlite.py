"""SQLite persistence for investigations and their event timeline.

The SQL is portable: `PostgresInvestigationStore` (app/storage/postgres.py) reuses
every query and only swaps the connection, placeholder style and row locking.

The agent snapshot is stored as an `InvestigationRecord` (the same model the CLI
exports as JSON); its events live in their own append-only table and are merged
back in on read, so there is one record format, not two. Lifecycle events added
by the service (`source = 'service'`) are interleaved with agent events by `seq`.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.agent.models import AgentEvent
from app.agent.runner import InvestigationRecord
from app.data.models import PublicCase
from app.evaluation.models import EvaluationResult, EvaluationStatus
from app.storage.models import (
    Approval,
    ApprovalDecision,
    DecisionConflictError,
    InvalidTransitionError,
    InvestigationStatus,
    InvestigationSummary,
    StepLease,
    StoredInvestigation,
    check_transition,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS investigations (
    investigation_id TEXT PRIMARY KEY,
    instance_id      TEXT NOT NULL,
    status           TEXT NOT NULL,
    case_json        TEXT NOT NULL,
    record_json      TEXT,
    error            TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS investigation_events (
    investigation_id TEXT NOT NULL REFERENCES investigations(investigation_id),
    seq              INTEGER NOT NULL,
    source           TEXT NOT NULL,
    event_json       TEXT NOT NULL,
    PRIMARY KEY (investigation_id, seq)
);
CREATE TABLE IF NOT EXISTS investigation_approvals (
    investigation_id TEXT PRIMARY KEY REFERENCES investigations(investigation_id),
    decision         TEXT NOT NULL,
    patch_id         TEXT NOT NULL,
    patch_sha256     TEXT NOT NULL,
    decided_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS investigation_evaluations (
    investigation_id TEXT PRIMARY KEY REFERENCES investigations(investigation_id),
    status           TEXT NOT NULL,
    result_json      TEXT NOT NULL,
    recorded_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS investigation_steps (
    investigation_id TEXT PRIMARY KEY REFERENCES investigations(investigation_id),
    lease_owner      TEXT,
    lease_expires_at TEXT,
    failures         INTEGER NOT NULL DEFAULT 0,
    steps            INTEGER NOT NULL DEFAULT 0
)
"""


class InvestigationNotFoundError(LookupError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _stamp(moment: datetime) -> str:
    # Fixed-width UTC timestamps, so lease expiry compares correctly as text in both databases.
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def schema_statements() -> list[str]:
    return [statement.strip() for statement in _SCHEMA.split(";") if statement.strip()]


class InvestigationStore:
    """SQLite store. Every method is one transaction."""

    backend = "sqlite"
    # Row lock taken before writes that read-then-insert (event seq, decisions).
    _lock_clause = ""
    _integrity_error: type[Exception] = sqlite3.IntegrityError

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            for statement in schema_statements():
                conn.execute(statement)

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        with closing(sqlite3.connect(self.path, timeout=30)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            with conn:  # one transaction per call
                yield conn

    # --- writes ---------------------------------------------------------------

    def create(self, investigation_id: str, case: PublicCase) -> StoredInvestigation:
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO investigations (investigation_id, instance_id, status, case_json,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    investigation_id,
                    case.instance_id,
                    InvestigationStatus.CREATED.value,
                    case.model_dump_json(),
                    now,
                    now,
                ),
            )
        return self.get(investigation_id)

    def transition(
        self,
        investigation_id: str,
        target: InvestigationStatus,
        *,
        error: str | None = None,
        event: AgentEvent | None = None,
    ) -> StoredInvestigation:
        """Move to `target`, raising InvalidTransitionError for illegal edges.

        `event` is appended in the same transaction, so readers never see the new
        status without the lifecycle event that explains it."""
        with self._connect() as conn:
            current = InvestigationStatus(self._row(conn, investigation_id, lock=True)["status"])
            check_transition(current, target)
            conn.execute(
                "UPDATE investigations SET status = ?, error = COALESCE(?, error),"
                " updated_at = ? WHERE investigation_id = ?",
                (target.value, error, _now(), investigation_id),
            )
            if event is not None:
                self._insert_events(conn, investigation_id, [event], source="service")
        return self.get(investigation_id)

    def save_record(self, record: InvestigationRecord) -> None:
        """Persist an agent snapshot and append any events not yet stored."""
        with self._connect() as conn:
            self._row(conn, record.investigation_id, lock=True)
            conn.execute(
                "UPDATE investigations SET record_json = ?, updated_at = ?"
                " WHERE investigation_id = ?",
                (
                    record.model_dump_json(exclude={"events"}),
                    _now(),
                    record.investigation_id,
                ),
            )
            stored = self._event_count(conn, record.investigation_id, source="agent")
            new = record.events[stored:]
            self._insert_events(conn, record.investigation_id, new, source="agent")

    def record_decision(
        self, investigation_id: str, decision: ApprovalDecision, *, event: AgentEvent
    ) -> StoredInvestigation:
        """Atomically record a human decision on the proposed patch and move the status.

        approve: AWAITING_APPROVAL -> PATCHING; reject: AWAITING_APPROVAL -> UNRESOLVED.
        Raises DecisionConflictError unless the investigation is awaiting approval of
        a proposed patch and has no decision yet.
        """
        target = (
            InvestigationStatus.PATCHING
            if decision == ApprovalDecision.APPROVE
            else InvestigationStatus.UNRESOLVED
        )
        with self._connect() as conn:
            row = self._row(conn, investigation_id, lock=True)
            current = InvestigationStatus(row["status"])
            if current != InvestigationStatus.AWAITING_APPROVAL:
                raise DecisionConflictError(f"investigation is {current}, not AWAITING_APPROVAL")
            check_transition(current, target)
            record = json.loads(row["record_json"]) if row["record_json"] else {}
            patch = record.get("proposed_patch")
            if not patch:
                raise DecisionConflictError("investigation has no proposed patch")
            try:
                conn.execute(
                    "INSERT INTO investigation_approvals (investigation_id, decision, patch_id,"
                    " patch_sha256, decided_at) VALUES (?, ?, ?, ?, ?)",
                    (investigation_id, decision.value, patch["patch_id"], patch["sha256"], _now()),
                )
            except self._integrity_error as exc:
                raise DecisionConflictError("a decision was already recorded") from exc
            conn.execute(
                "UPDATE investigations SET status = ?, updated_at = ? WHERE investigation_id = ?",
                (target.value, _now(), investigation_id),
            )
            self._insert_events(conn, investigation_id, [event], source="service")
        return self.get(investigation_id)

    def record_evaluation(
        self, investigation_id: str, result: EvaluationResult, *, event: AgentEvent
    ) -> StoredInvestigation:
        """Atomically store the evaluation result and close the case.

        EVALUATING -> RESOLVED only when the harness reported the issue resolved;
        every other outcome (failed tests, errors, timeouts) -> UNRESOLVED."""
        target = (
            InvestigationStatus.RESOLVED
            if result.status == EvaluationStatus.RESOLVED and result.resolved is True
            else InvestigationStatus.UNRESOLVED
        )
        with self._connect() as conn:
            current = InvestigationStatus(self._row(conn, investigation_id, lock=True)["status"])
            if current != InvestigationStatus.EVALUATING:
                raise InvalidTransitionError(current, target)
            conn.execute(
                "INSERT INTO investigation_evaluations (investigation_id, status, result_json,"
                " recorded_at) VALUES (?, ?, ?, ?)",
                (investigation_id, result.status.value, result.model_dump_json(), _now()),
            )
            conn.execute(
                "UPDATE investigations SET status = ?, updated_at = ? WHERE investigation_id = ?",
                (target.value, _now(), investigation_id),
            )
            self._insert_events(conn, investigation_id, [event], source="service")
        return self.get(investigation_id)

    def append_event(self, investigation_id: str, event: AgentEvent) -> None:
        with self._connect() as conn:
            self._row(conn, investigation_id, lock=True)
            self._insert_events(conn, investigation_id, [event], source="service")

    # --- stepwise execution leases ---------------------------------------------

    def acquire_step(
        self, investigation_id: str, owner: str, lease_seconds: float
    ) -> StepLease | None:
        """Take the run's step lease, or None while another request holds a live one.

        A lease that expired without being released means the process driving that
        step died (or was killed by the platform); it counts as a failed step, so
        repeated crashes stay bounded by `failures`."""
        now = datetime.now(UTC)
        with self._connect() as conn:
            self._row(conn, investigation_id, lock=True)
            conn.execute(
                "INSERT INTO investigation_steps (investigation_id, failures, steps)"
                " VALUES (?, 0, 0) ON CONFLICT (investigation_id) DO NOTHING",
                (investigation_id,),
            )
            row = conn.execute(
                "SELECT lease_owner, lease_expires_at, failures, steps"
                " FROM investigation_steps WHERE investigation_id = ?",
                (investigation_id,),
            ).fetchone()
            if row["lease_owner"] is not None and row["lease_expires_at"] > _stamp(now):
                return None
            abandoned = row["lease_owner"] is not None
            failures = row["failures"] + (1 if abandoned else 0)
            conn.execute(
                "UPDATE investigation_steps SET lease_owner = ?, lease_expires_at = ?,"
                " failures = ?, steps = steps + 1 WHERE investigation_id = ?",
                (
                    owner,
                    _stamp(now + timedelta(seconds=lease_seconds)),
                    failures,
                    investigation_id,
                ),
            )
        return StepLease(
            owner=owner, failures=failures, steps=row["steps"] + 1, abandoned=abandoned
        )

    def release_step(self, investigation_id: str, owner: str, *, failed: bool) -> int:
        """Release a lease this owner holds; returns the consecutive failure count."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT lease_owner, failures FROM investigation_steps WHERE investigation_id = ?",
                (investigation_id,),
            ).fetchone()
            if row is None or row["lease_owner"] != owner:
                return row["failures"] if row is not None else 0
            failures = row["failures"] + 1 if failed else 0
            conn.execute(
                "UPDATE investigation_steps SET lease_owner = NULL, lease_expires_at = NULL,"
                " failures = ? WHERE investigation_id = ? AND lease_owner = ?",
                (failures, investigation_id, owner),
            )
        return failures

    # --- reads ----------------------------------------------------------------

    def get(self, investigation_id: str) -> StoredInvestigation:
        with self._connect() as conn:
            row = self._row(conn, investigation_id)
            events = self._events(conn, investigation_id)
            approval_row = conn.execute(
                "SELECT * FROM investigation_approvals WHERE investigation_id = ?",
                (investigation_id,),
            ).fetchone()
            evaluation_row = conn.execute(
                "SELECT result_json FROM investigation_evaluations WHERE investigation_id = ?",
                (investigation_id,),
            ).fetchone()
        approval = None
        if approval_row is not None:
            approval = Approval(
                decision=ApprovalDecision(approval_row["decision"]),
                patch_id=approval_row["patch_id"],
                patch_sha256=approval_row["patch_sha256"],
                decided_at=datetime.fromisoformat(approval_row["decided_at"]),
            )
        record = None
        if row["record_json"] is not None:
            data = json.loads(row["record_json"])
            data["events"] = [e.model_dump(mode="json") for e in events]
            record = InvestigationRecord.model_validate(data)
        return StoredInvestigation(
            investigation_id=row["investigation_id"],
            status=InvestigationStatus(row["status"]),
            case=PublicCase.model_validate_json(row["case_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            error=row["error"],
            events=events,
            record=record,
            approval=approval,
            evaluation=(
                EvaluationResult.model_validate_json(evaluation_row["result_json"])
                if evaluation_row is not None
                else None
            ),
        )

    def events(self, investigation_id: str) -> list[AgentEvent]:
        with self._connect() as conn:
            self._row(conn, investigation_id)
            return self._events(conn, investigation_id)

    def list_recent(self, limit: int = 20) -> list[InvestigationSummary]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT investigation_id, instance_id, status, created_at, updated_at"
                " FROM investigations ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            InvestigationSummary(
                investigation_id=r["investigation_id"],
                instance_id=r["instance_id"],
                status=InvestigationStatus(r["status"]),
                created_at=datetime.fromisoformat(r["created_at"]),
                updated_at=datetime.fromisoformat(r["updated_at"]),
            )
            for r in rows
        ]

    def ids_with_status(self, statuses: frozenset[InvestigationStatus]) -> list[str]:
        marks = ",".join("?" * len(statuses))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT investigation_id FROM investigations WHERE status IN ({marks})"  # noqa: S608
                " ORDER BY created_at",
                [s.value for s in statuses],
            ).fetchall()
        return [r["investigation_id"] for r in rows]

    # --- helpers --------------------------------------------------------------

    def _row(self, conn: Any, investigation_id: str, *, lock: bool = False) -> Any:
        query = "SELECT * FROM investigations WHERE investigation_id = ?"
        if lock:
            query += self._lock_clause
        row = conn.execute(query, (investigation_id,)).fetchone()
        if row is None:
            raise InvestigationNotFoundError(investigation_id)
        return row

    @staticmethod
    def _event_count(conn: Any, investigation_id: str, source: str) -> int:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM investigation_events"
            " WHERE investigation_id = ? AND source = ?",
            (investigation_id, source),
        ).fetchone()["n"]

    @staticmethod
    def _insert_events(
        conn: Any, investigation_id: str, events: list[AgentEvent], source: str
    ) -> None:
        if not events:
            return
        start = conn.execute(
            "SELECT COALESCE(MAX(seq) + 1, 0) AS next FROM investigation_events"
            " WHERE investigation_id = ?",
            (investigation_id,),
        ).fetchone()["next"]
        conn.executemany(
            "INSERT INTO investigation_events (investigation_id, seq, source, event_json)"
            " VALUES (?, ?, ?, ?)",
            [
                (investigation_id, start + i, source, e.model_dump_json())
                for i, e in enumerate(events)
            ],
        )

    @staticmethod
    def _events(conn: Any, investigation_id: str) -> list[AgentEvent]:
        rows = conn.execute(
            "SELECT event_json FROM investigation_events WHERE investigation_id = ? ORDER BY seq",
            (investigation_id,),
        ).fetchall()
        return [AgentEvent.model_validate_json(r["event_json"]) for r in rows]

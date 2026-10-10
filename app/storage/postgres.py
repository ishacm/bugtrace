"""Postgres persistence for hosted (serverless) deployments.

Same schema, queries and transaction boundaries as the SQLite store; only the
connection, the placeholder style and row locking differ. Every call opens a
short-lived connection, which suits serverless functions that may be frozen or
destroyed between requests. Prepared statements are disabled, so transaction-
pooling endpoints (PgBouncer-style poolers of free hosted providers) work too.

psycopg is imported lazily: local SQLite mode never loads it.
"""

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from app.storage.sqlite import InvestigationStore

CONNECT_TIMEOUT_SECONDS = 10


def to_pyformat(query: str) -> str:
    """Translate the store's `?` placeholders to psycopg's `%s` (escaping literal `%`)."""
    return query.replace("%", "%%").replace("?", "%s")


class _Connection:
    """Gives a psycopg connection the small sqlite3-style surface the store uses."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def execute(self, query: str, params: Sequence[Any] = ()) -> Any:
        return self._conn.execute(to_pyformat(query), params)

    def executemany(self, query: str, rows: Sequence[Sequence[Any]]) -> None:
        with self._conn.cursor() as cursor:
            cursor.executemany(to_pyformat(query), rows)


def _psycopg_connect(url: str) -> Any:
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(
        url,
        row_factory=dict_row,
        prepare_threshold=None,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        application_name="bugtrace",
    )


def _integrity_error() -> type[Exception]:
    import psycopg

    return psycopg.IntegrityError


class PostgresInvestigationStore(InvestigationStore):
    backend = "postgres"
    _lock_clause = " FOR UPDATE"

    def __init__(
        self,
        url: str,
        *,
        connect: Callable[[str], Any] | None = None,
        integrity_error: type[Exception] | None = None,
    ) -> None:
        # The URL holds credentials: it is kept private and never logged or rendered.
        self._url = url
        self._connect_fn = connect or _psycopg_connect
        self._integrity_error = integrity_error or _integrity_error()
        self._init_schema()

    def __repr__(self) -> str:
        return "PostgresInvestigationStore(<DATABASE_URL>)"

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        # psycopg: `with connection` commits on success, rolls back on error, then closes.
        with self._connect_fn(self._url) as conn:
            yield _Connection(conn)

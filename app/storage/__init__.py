"""Investigation persistence. `open_store` picks SQLite or Postgres from settings."""

from app.config import Settings
from app.storage.sqlite import InvestigationStore


def open_store(settings: Settings) -> InvestigationStore:
    """The configured store. Postgres never falls back to SQLite: a missing or invalid
    DATABASE_URL already failed in `Settings`, and connection errors propagate."""
    if settings.storage_backend == "postgres":
        from app.storage.postgres import PostgresInvestigationStore

        if settings.database_url is None:  # unreachable after Settings validation
            raise RuntimeError("Postgres storage needs DATABASE_URL")
        return PostgresInvestigationStore(settings.database_url.get_secret_value())
    return InvestigationStore(settings.database_path)

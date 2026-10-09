"""SWE-bench adapter: converts raw rows into agent-safe `PublicCase` objects."""

from collections.abc import Iterable, Mapping
from typing import Any

from app.data.models import PUBLIC_CASE_FIELDS, PublicCase
from app.data.source import fetch_raw_rows


def public_case_from_row(row: Mapping[str, Any]) -> PublicCase:
    """Project a raw row onto the public whitelist before validation."""
    safe = {key: row[key] for key in PUBLIC_CASE_FIELDS if row.get(key) is not None}
    return PublicCase.model_validate(safe)


def public_cases_from_rows(rows: Iterable[Mapping[str, Any]]) -> list[PublicCase]:
    return [public_case_from_row(row) for row in rows]


def load_public_cases(dataset_id: str, split: str, limit: int) -> list[PublicCase]:
    return public_cases_from_rows(fetch_raw_rows(dataset_id, split, limit))

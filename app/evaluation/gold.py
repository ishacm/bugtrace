"""Evaluator-only gold data. Agent modules must never import this module."""

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.data.source import DatasetFetchError, find_raw_row


class GoldCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: str
    patch: str
    test_patch: str
    fail_to_pass: list[str]
    pass_to_pass: list[str]


def _test_list(value: Any) -> list[str]:
    # SWE-bench stores test lists as JSON-encoded strings.
    if isinstance(value, str):
        value = json.loads(value)
    return [str(item) for item in value]


def gold_case_from_row(row: Mapping[str, Any]) -> GoldCase:
    return GoldCase(
        instance_id=row["instance_id"],
        patch=row["patch"],
        test_patch=row["test_patch"],
        fail_to_pass=_test_list(row["FAIL_TO_PASS"]),
        pass_to_pass=_test_list(row["PASS_TO_PASS"]),
    )


_INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,200}$")
_DIFF_PATH_RE = re.compile(r"^(?:---|\+\+\+) (?:a|b)/(\S+)")


class ReferenceUnavailableError(RuntimeError):
    pass


def modified_files(diff: str) -> list[str]:
    """Files a unified diff touches, in order of first appearance."""
    files: list[str] = []
    for line in diff.splitlines():
        match = _DIFF_PATH_RE.match(line)
        if match and match.group(1) not in files:
            files.append(match.group(1))
    return files


def row_sha256(row: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True).encode("utf-8")).hexdigest()


class GoldSource:
    """Loads reference rows by `instance_id`, caching them under the evaluator dir.

    The cache keeps reruns reproducible (same row, same hash) and offline. The
    cache directory is evaluator-only; no agent tool can read it.
    """

    def __init__(
        self,
        cache_dir: Path,
        dataset: str,
        split: str,
        fetch: Callable[[str, str, str], Mapping[str, Any]] = find_raw_row,
    ) -> None:
        self.cache_dir = cache_dir
        self.dataset = dataset
        self.split = split
        self.fetch = fetch

    def _path(self, instance_id: str) -> Path:
        if not _INSTANCE_ID_RE.fullmatch(instance_id) or instance_id.startswith("."):
            raise ReferenceUnavailableError(f"invalid instance id {instance_id!r}")
        return self.cache_dir / f"{instance_id}.json"

    def row(self, instance_id: str) -> dict[str, Any]:
        path = self._path(instance_id)
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        try:
            row = dict(self.fetch(self.dataset, self.split, instance_id))
        except DatasetFetchError as exc:
            raise ReferenceUnavailableError(str(exc)) from exc
        if row.get("instance_id") != instance_id:
            raise ReferenceUnavailableError(f"reference row for {instance_id} not found")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(row, sort_keys=True), encoding="utf-8")
        return row

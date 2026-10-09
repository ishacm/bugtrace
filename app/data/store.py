"""Local JSONL store of seeded public cases."""

import json
from collections.abc import Iterable
from pathlib import Path

from app.data.models import PublicCase


def write_cases(path: Path, cases: Iterable[PublicCase]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case.model_dump(mode="json"), ensure_ascii=False) + "\n")
            count += 1
    return count


def read_cases(path: Path) -> list[PublicCase]:
    """Read seeded cases. Unknown keys (e.g. leaked gold fields) fail validation."""
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as handle:
        return [PublicCase.model_validate_json(line) for line in handle if line.strip()]

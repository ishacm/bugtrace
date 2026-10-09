"""Evaluator-only gold data. Agent modules must never import this module."""

import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict


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

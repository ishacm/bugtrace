"""Opt-in integration against a real SWE-bench Verified case.

Skipped unless SWEBENCH_INTEGRATION=1 (`make eval-integration`). The reference test
needs network access; the harness test also needs a running Docker daemon and
downloads the case's evaluation image (x86_64; emulated on Apple Silicon).

The oracle test submits the benchmark's own reference fix as the candidate. That
is a check of the evaluation wrapper itself and stays inside the evaluator
boundary; the agent is not involved.
"""

import asyncio
import os
from pathlib import Path

import pytest

from app.config import Settings
from app.evaluation.evaluator import SwebenchEvaluator, docker_preflight
from app.evaluation.gold import gold_case_from_row, modified_files
from app.evaluation.models import EvaluationRequest, EvaluationStatus

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("SWEBENCH_INTEGRATION") != "1", reason="set SWEBENCH_INTEGRATION=1"
    ),
]

INSTANCE_ID = "astropy__astropy-12907"
BASE_COMMIT = "d16bfe05a744909de4b27f5875fe0d4ed41ce607"


def _evaluator(tmp_path: Path) -> SwebenchEvaluator:
    settings = Settings(_env_file=None, evaluation_dir=tmp_path / "evaluations")
    return SwebenchEvaluator.from_settings(settings)


def test_real_reference_row_matches_seeded_case(tmp_path: Path) -> None:
    evaluator = _evaluator(tmp_path)
    row = evaluator.gold.row(INSTANCE_ID)
    assert row["base_commit"] == BASE_COMMIT
    assert {"image", "eval_script", "log_parser"} <= row.keys()  # harness 5.x fields
    assert modified_files(gold_case_from_row(row).patch)
    assert evaluator.gold.row(INSTANCE_ID) == row  # served from the cache


def test_oracle_patch_resolves_real_case(tmp_path: Path) -> None:
    unavailable = docker_preflight()
    if unavailable:
        pytest.skip(unavailable)
    evaluator = _evaluator(tmp_path)
    reference = gold_case_from_row(evaluator.gold.row(INSTANCE_ID))
    files = modified_files(reference.patch)
    request = EvaluationRequest(
        investigation_id="00000000-0000-4000-8000-000000000009",
        instance_id=INSTANCE_ID,
        base_commit=BASE_COMMIT,
        patch_id="oracle",
        patch_sha256="0" * 64,
        candidate_diff=reference.patch,
        suspect_files=files,
        changed_files=files,
    )
    result = asyncio.run(evaluator.evaluate(request))
    assert result.status == EvaluationStatus.RESOLVED, result.message
    assert result.resolved is True and result.target_tests.failed == 0

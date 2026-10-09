"""Wrapper around the official SWE-bench harness (`swebench.harness.run_evaluation`).

The harness runs in its own ephemeral Python environment (`uv run --isolated`
with a pinned `swebench` version), so its heavy dependencies never enter
BugTrace's environment. It applies the candidate patch and the benchmark's test
patch inside a fresh Docker container per instance; the investigation workspace
is never used for grading.

Everything under a run directory is evaluator-only: it contains the reference
row and raw test logs. Parsed outcomes keep counts and flags, never test names.
"""

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import signal
import subprocess  # noqa: S404
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.evaluation.models import EvaluationStatus, FailureReason, PassFailCounts
from app.workspace.runner import CommandError, run_command

MODEL_NAME = "bugtrace"
HARNESS_MODULE = "swebench.harness.run_evaluation"

# Markers the harness writes to its per-instance logs (swebench.harness.constants).
APPLY_PATCH_FAIL = ">>>>> Patch Apply Failed"
TESTS_TIMEOUT = "Timeout error:"

# Never hand provider credentials or BugTrace settings to the harness process.
_SCRUBBED_ENV = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS")
_SCRUBBED_PREFIXES = ("BUGTRACE_", "GIT_")


class Prediction(BaseModel):
    """One line of a SWE-bench predictions file."""

    instance_id: str
    model_name_or_path: str = MODEL_NAME
    model_patch: str


class HarnessOutcome(BaseModel):
    status: EvaluationStatus
    message: str
    resolved: bool | None = None
    failure_reason: FailureReason | None = None
    patch_applied: bool | None = None
    target_tests: PassFailCounts | None = None
    regression_tests: PassFailCounts | None = None


def write_predictions(path: Path, predictions: Sequence[Prediction]) -> str:
    """Write SWE-bench JSONL predictions; returns the file's sha256."""
    text = "".join(p.model_dump_json() + "\n" for p in predictions)
    path.write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def default_command_prefix(harness_version: str, python: str) -> list[str] | None:
    """`uv run` in an isolated env with the pinned harness, or None if uv is missing."""
    uv = shutil.which("uv")
    if uv is None:
        return None
    return [
        uv,
        "run",
        "--isolated",
        "--no-project",
        "--python",
        python,
        "--with",
        f"swebench=={harness_version}",
        "python",
        "-m",
        HARNESS_MODULE,
    ]


def harness_args(
    prefix: Sequence[str],
    *,
    dataset_path: Path,
    split: str,
    instance_id: str,
    predictions_path: Path,
    run_id: str,
    test_timeout: int,
    report_dir: Path,
) -> list[str]:
    return [
        *prefix,
        "--dataset_name",
        str(dataset_path),
        "--split",
        split,
        "--instance_ids",
        instance_id,
        "--predictions_path",
        str(predictions_path),
        "--max_workers",
        "1",
        "--run_id",
        run_id,
        "--timeout",
        str(test_timeout),
        "--report_dir",
        str(report_dir),
    ]


def harness_env() -> dict[str, str]:
    return {
        k: v
        for k, v in os.environ.items()
        if k not in _SCRUBBED_ENV and not k.startswith(_SCRUBBED_PREFIXES)
    }


async def run_harness(
    args: Sequence[str], *, cwd: Path, timeout: float, log_path: Path
) -> tuple[int | None, bool]:
    """Run the harness without a shell; returns (exit code, timed out).

    Output goes to an evaluator-only log file. The process group is killed on
    timeout or cancellation so no harness process outlives the evaluation."""
    if isinstance(args, str) or not args:
        raise ValueError("args must be a non-empty sequence of strings")
    if not cwd.is_absolute() or not cwd.is_dir():
        raise ValueError(f"cwd must be an existing absolute directory: {cwd}")
    with log_path.open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            env=harness_env(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            return await asyncio.wait_for(process.wait(), timeout), False
        except TimeoutError:
            _kill(process)
            await process.wait()
            return None, True
        except asyncio.CancelledError:
            _kill(process)
            with contextlib.suppress(Exception):
                await asyncio.shield(process.wait())
            raise


def _kill(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)


def remove_run_containers(run_id: str, cwd: Path) -> None:
    """Best effort: remove containers a killed harness run left behind."""
    if shutil.which("docker") is None:
        return
    try:
        listed = run_command(
            ["docker", "ps", "-aq", "--filter", f"name={run_id}"], cwd=cwd, timeout=30
        )
        ids = listed.stdout.split()
        if ids:
            run_command(["docker", "rm", "-f", *ids], cwd=cwd, timeout=120, check=False)
    except (CommandError, ValueError):
        pass


def instance_log_dir(run_dir: Path, run_id: str, instance_id: str) -> Path:
    return run_dir / "logs" / "run_evaluation" / run_id / MODEL_NAME / instance_id


def _counts(tests_status: dict[str, Any], key: str) -> PassFailCounts | None:
    group = tests_status.get(key)
    if not isinstance(group, dict):
        return None
    return PassFailCounts(
        passed=len(group.get("success") or []), failed=len(group.get("failure") or [])
    )


def _contains(path: Path, marker: str) -> bool:
    try:
        return marker in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def parse_outcome(
    *, run_dir: Path, report_dir: Path, run_id: str, instance_id: str, exit_code: int | None
) -> HarnessOutcome:
    """Turn the harness's report files into a verdict.

    Only the harness's own `resolved` flag decides RESOLVED; anything the harness
    could not grade is an ERROR, never a guess."""
    log_dir = instance_log_dir(run_dir, run_id, instance_id)
    summary_path = report_dir / f"{MODEL_NAME}.{run_id}.json"
    summary: dict[str, Any] = {}
    if summary_path.is_file():
        with contextlib.suppress(ValueError):
            summary = json.loads(summary_path.read_text(encoding="utf-8"))

    def in_summary(key: str) -> bool:
        return instance_id in (summary.get(key) or [])

    report: dict[str, Any] | None = None
    report_path = log_dir / "report.json"
    if report_path.is_file():
        with contextlib.suppress(ValueError, AttributeError):
            report = json.loads(report_path.read_text(encoding="utf-8")).get(instance_id)

    if report is None:
        if _contains(log_dir / "run_instance.log", APPLY_PATCH_FAIL):
            return HarnessOutcome(
                status=EvaluationStatus.UNRESOLVED,
                resolved=False,
                patch_applied=False,
                failure_reason="PATCH_APPLY_FAILED",
                message="The patch did not apply in the clean evaluation environment.",
            )
        if _contains(log_dir / "test_output.txt", TESTS_TIMEOUT):
            return HarnessOutcome(
                status=EvaluationStatus.TIMEOUT,
                patch_applied=True,
                failure_reason="TESTS_TIMED_OUT",
                message="The benchmark tests exceeded the per-instance timeout.",
            )
        if in_summary("infra_failure_ids") or in_summary("ambiguous_failure_ids"):
            return HarnessOutcome(
                status=EvaluationStatus.ERROR,
                failure_reason="INFRA_FAILURE",
                message="The evaluation environment failed before tests could be graded.",
            )
        if exit_code not in (0, None) or not summary:
            return HarnessOutcome(
                status=EvaluationStatus.ERROR,
                failure_reason="HARNESS_FAILED",
                message=f"The SWE-bench harness exited with code {exit_code} without a report.",
            )
        return HarnessOutcome(
            status=EvaluationStatus.ERROR,
            failure_reason="REPORT_MISSING",
            message="The SWE-bench harness finished without a report for this instance.",
        )

    tests_status = report.get("tests_status") or {}
    applied = bool(report.get("patch_successfully_applied"))
    target = _counts(tests_status, "FAIL_TO_PASS")
    regression = _counts(tests_status, "PASS_TO_PASS")
    if not applied:
        if report.get("infra_failure"):
            return HarnessOutcome(
                status=EvaluationStatus.ERROR,
                failure_reason="INFRA_FAILURE",
                message="The test run produced no gradable output (environment failure).",
            )
        return HarnessOutcome(
            status=EvaluationStatus.ERROR,
            failure_reason="REPORT_MISSING",
            message="The test run produced no gradable output.",
        )
    if report.get("resolved") is True:
        return HarnessOutcome(
            status=EvaluationStatus.RESOLVED,
            resolved=True,
            patch_applied=True,
            target_tests=target,
            regression_tests=regression,
            message="SWE-bench tests pass: the issue is resolved with no regressions.",
        )
    return HarnessOutcome(
        status=EvaluationStatus.UNRESOLVED,
        resolved=False,
        patch_applied=True,
        target_tests=target,
        regression_tests=regression,
        message="SWE-bench tests ran, but the patch does not resolve the issue.",
    )

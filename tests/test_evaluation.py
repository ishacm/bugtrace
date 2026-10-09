"""SWE-bench evaluation boundary (fake harness subprocess; no Docker, network or Gemini)."""

import asyncio
import json
import os
import sqlite3
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agent.models import AgentEvent
from app.evaluation.evaluator import EvaluationRefusedError, SwebenchEvaluator, build_request
from app.evaluation.gold import GoldSource, ReferenceUnavailableError, modified_files
from app.evaluation.harness import (
    MODEL_NAME,
    Prediction,
    default_command_prefix,
    harness_args,
    harness_env,
    parse_outcome,
    write_predictions,
)
from app.evaluation.metrics import localization_metrics
from app.evaluation.models import EvaluationRequest, EvaluationStatus
from app.storage.models import TERMINAL_STATUSES, InvestigationStatus
from app.storage.sqlite import InvestigationStore
from tests import test_patches
from tests.test_investigations_api import INSTANCE_ID, StaticEvaluator, _assert_no_gold, _wait_for
from tests.test_patches import (
    FIX_LINE,
    GOOD_DIFF,
    _app,
    _awaiting,
    _backend,
    _decide,
    _repo,
    _store,
)

penv = test_patches.penv  # shared fixture

S = InvestigationStatus
FAKE_HARNESS = Path(__file__).resolve().parent / "fixtures" / "fake_swebench_harness.py"
FIX_MARKER = f"+{FIX_LINE}\n"
BASE = "d16bfe05a744909de4b27f5875fe0d4ed41ce607"


def _reference_row(base_commit: str = BASE) -> dict[str, Any]:
    return {
        "instance_id": INSTANCE_ID,
        "repo": "owner/fixture",
        "base_commit": base_commit,
        "problem_statement": "stmt",
        "patch": "--- a/pkg/core.py\n+++ b/pkg/core.py\n@@ GOLD_PATCH\n"
        "--- a/pkg/helpers_gold.py\n+++ b/pkg/helpers_gold.py\n@@ GOLD\n",
        "test_patch": "--- a/tests/test_core.py\n+++ b/tests/test_core.py\n+GOLD_TEST\n",
        "FAIL_TO_PASS": '["tests/test_core.py::GOLD_F2P_a", "tests/test_core.py::GOLD_F2P_b"]',
        "PASS_TO_PASS": '["tests/test_core.py::GOLD_P2P"]',
        "hints_text": "GOLD_HINT",
        "eval_script": "GOLD_EVAL_SCRIPT",
        "image": "swebench/sweb.eval.x86_64.fixture:latest",
        "version": "1.0",
    }


def _evaluator(
    tmp_path: Path,
    row: dict[str, Any] | None = None,
    *,
    preflight: Callable[[], str | None] = lambda: None,
    timeout: float = 60,
    fetch: Callable[[str, str, str], dict[str, Any]] | None = None,
) -> SwebenchEvaluator:
    row = row or _reference_row()
    gold = GoldSource(
        tmp_path / "evaluations" / "reference",
        "test/dataset",
        "test",
        fetch=fetch or (lambda _d, _s, _i: row),
    )
    return SwebenchEvaluator(
        gold=gold,
        runs_dir=tmp_path / "evaluations" / "runs",
        harness_version="0.0-test",
        python="3.12",
        timeout_seconds=timeout,
        test_timeout_seconds=5,
        command_prefix=[sys.executable, str(FAKE_HARNESS)],
        preflight=preflight,
    )


def _request(
    diff: str = GOOD_DIFF,
    *,
    suspects: tuple[str, ...] = ("pkg/core.py", "pkg/usage.py"),
    changed: tuple[str, ...] = ("pkg/core.py",),
    base: str = BASE,
) -> EvaluationRequest:
    return EvaluationRequest(
        investigation_id=str(uuid.uuid4()),
        instance_id=INSTANCE_ID,
        base_commit=base,
        patch_id="patch-1",
        patch_sha256="0" * 64,
        candidate_diff=diff,
        suspect_files=list(suspects),
        changed_files=list(changed),
    )


@pytest.fixture
def harness_mode(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    monkeypatch.setenv("FAKE_HARNESS_FIX", FIX_MARKER)

    def set_mode(mode: str) -> None:
        monkeypatch.setenv("FAKE_HARNESS_MODE", mode)

    set_mode("grade")
    return set_mode


def _no_gold_text(payload: Any) -> None:
    text = json.dumps(payload)
    assert "GOLD" not in text, text
    assert "helpers_gold" not in text


def _run_dir(tmp_path: Path) -> Path:
    runs = list((tmp_path / "evaluations" / "runs").glob("*/*"))
    assert len(runs) == 1
    return runs[0]


# --- predictions, command, reference data ------------------------------------------------


def test_prediction_file_is_swebench_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "predictions.jsonl"
    sha = write_predictions(path, [Prediction(instance_id=INSTANCE_ID, model_patch=GOOD_DIFF)])
    lines = path.read_text().splitlines()
    assert len(lines) == 1 and len(sha) == 64
    assert json.loads(lines[0]) == {
        "instance_id": INSTANCE_ID,
        "model_name_or_path": MODEL_NAME,
        "model_patch": GOOD_DIFF,
    }


def test_harness_command_is_an_argument_list_with_pinned_version(tmp_path: Path) -> None:
    prefix = default_command_prefix("5.0.2", "3.12")
    if prefix is None:
        pytest.skip("uv not on PATH")
    args = harness_args(
        prefix,
        dataset_path=tmp_path / "dataset.json",
        split="test",
        instance_id=INSTANCE_ID,
        predictions_path=tmp_path / "p.jsonl",
        run_id="run-1",
        test_timeout=30,
        report_dir=tmp_path / "reports",
    )
    assert all(isinstance(a, str) for a in args)
    assert "--isolated" in args and "swebench==5.0.2" in args
    assert args[args.index("--instance_ids") + 1] == INSTANCE_ID
    assert args[args.index("--max_workers") + 1] == "1"


def test_harness_env_drops_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "secret")
    monkeypatch.setenv("BUGTRACE_MODEL_NAME", "m")
    monkeypatch.setenv("GIT_DIR", "/elsewhere")
    env = harness_env()
    assert not {"GEMINI_API_KEY", "BUGTRACE_MODEL_NAME", "GIT_DIR"} & env.keys()


def test_modified_files_reads_diff_headers() -> None:
    assert modified_files(GOOD_DIFF) == ["pkg/core.py"]
    assert modified_files(_reference_row()["patch"]) == ["pkg/core.py", "pkg/helpers_gold.py"]


def test_localization_compares_suspects_to_reference_files() -> None:
    reference = ["pkg/core.py", "pkg/other.py"]
    hit = localization_metrics(reference, ["pkg/core.py", "a.py", "pkg/other.py"], ["pkg/core.py"])
    assert hit.top1_file_hit and hit.top3_file_recall == 1.0
    assert hit.reference_file_count == 2 and hit.candidate_file_overlap == 1

    miss = localization_metrics(reference, ["a.py", "b.py", "c.py", "pkg/core.py"], ["a.py"])
    assert not miss.top1_file_hit and miss.top3_file_recall == 0.0
    assert miss.candidate_file_overlap == 0

    partial = localization_metrics(reference, ["a.py", "pkg/other.py"], [])
    assert partial.top3_file_recall == 0.5
    assert not localization_metrics(reference, [], []).top1_file_hit


def test_gold_source_caches_rows_and_rejects_bad_ids(tmp_path: Path) -> None:
    calls: list[str] = []

    def fetch(_dataset: str, _split: str, instance_id: str) -> dict[str, Any]:
        calls.append(instance_id)
        return _reference_row()

    source = GoldSource(tmp_path / "ref", "d", "test", fetch=fetch)
    assert source.row(INSTANCE_ID)["patch"].startswith("--- a/pkg/core.py")
    assert source.row(INSTANCE_ID) == source.row(INSTANCE_ID)
    assert calls == [INSTANCE_ID]  # cached after the first fetch
    for bad in ("../escape", "a/b", "", ".hidden"):
        with pytest.raises(ReferenceUnavailableError):
            source.row(bad)
    wrong = GoldSource(tmp_path / "ref2", "d", "test", fetch=lambda *_: {"instance_id": "x"})
    with pytest.raises(ReferenceUnavailableError):
        wrong.row(INSTANCE_ID)


# --- report parsing ------------------------------------------------------------------------


def _write_report(run_dir: Path, run_id: str, report: dict[str, Any] | None) -> None:
    log_dir = run_dir / "logs" / "run_evaluation" / run_id / MODEL_NAME / INSTANCE_ID
    log_dir.mkdir(parents=True)
    if report is not None:
        (log_dir / "report.json").write_text(json.dumps({INSTANCE_ID: report}))


def _parse(run_dir: Path, exit_code: int | None = 0) -> Any:
    return parse_outcome(
        run_dir=run_dir,
        report_dir=run_dir / "reports",
        run_id="r",
        instance_id=INSTANCE_ID,
        exit_code=exit_code,
    )


def test_parse_counts_tests_without_names(tmp_path: Path) -> None:
    status = {
        "FAIL_TO_PASS": {"success": ["GOLD_a"], "failure": ["GOLD_b"]},
        "PASS_TO_PASS": {"success": ["GOLD_c", "GOLD_d"], "failure": []},
    }
    _write_report(
        tmp_path,
        "r",
        {"patch_successfully_applied": True, "resolved": False, "tests_status": status},
    )
    outcome = _parse(tmp_path)
    assert outcome.status == EvaluationStatus.UNRESOLVED and outcome.resolved is False
    assert (outcome.target_tests.passed, outcome.target_tests.failed) == (1, 1)
    assert (outcome.regression_tests.passed, outcome.regression_tests.failed) == (2, 0)
    _no_gold_text(outcome.model_dump(mode="json"))


@pytest.mark.parametrize(
    ("report", "exit_code", "status", "reason"),
    [
        ({"patch_successfully_applied": True, "resolved": True}, 0, "RESOLVED", None),
        ({"patch_successfully_applied": False, "infra_failure": True}, 0, "ERROR", "INFRA_FAILURE"),
        ({"patch_successfully_applied": False}, 0, "ERROR", "REPORT_MISSING"),
        (None, 1, "ERROR", "HARNESS_FAILED"),
        (None, 0, "ERROR", "HARNESS_FAILED"),  # no summary either
    ],
)
def test_parse_outcomes(
    tmp_path: Path, report: dict | None, exit_code: int, status: str, reason: str | None
) -> None:
    _write_report(tmp_path, "r", report)
    outcome = _parse(tmp_path, exit_code)
    assert (outcome.status, outcome.failure_reason) == (status, reason)
    assert outcome.resolved is (True if status == "RESOLVED" else outcome.resolved)
    if status != "RESOLVED":
        assert outcome.resolved is not True


# --- evaluator with the fake harness subprocess ---------------------------------------------


def test_resolved_verdict_comes_from_harness_tests(
    tmp_path: Path, harness_mode: Callable[[str], None]
) -> None:
    result = asyncio.run(_evaluator(tmp_path).evaluate(_request()))
    assert result.status == EvaluationStatus.RESOLVED and result.resolved is True
    assert result.patch_applied_in_evaluator is True
    assert (result.target_tests.passed, result.target_tests.failed) == (2, 0)
    assert (result.regression_tests.passed, result.regression_tests.failed) == (1, 0)
    assert result.localization.top1_file_hit and result.localization.top3_file_recall == 0.5
    assert result.safety.unauthorized_writes == 0
    assert result.harness.exit_code == 0 and result.harness.prediction_sha256
    _no_gold_text(result.model_dump(mode="json"))

    run_dir = _run_dir(tmp_path)
    prediction = json.loads((run_dir / "predictions.jsonl").read_text())
    assert prediction["model_patch"] == GOOD_DIFF  # only the candidate is submitted
    argv = json.loads((run_dir / "argv.json").read_text())
    assert argv[argv.index("--dataset_name") + 1] == str(run_dir / "dataset.json")
    assert (run_dir / "manifest.json").is_file()


def test_unresolved_when_tests_fail(tmp_path: Path, harness_mode: Callable[[str], None]) -> None:
    other = GOOD_DIFF.replace(f"+{FIX_LINE}", "+    return sum(items) - 1")
    result = asyncio.run(_evaluator(tmp_path).evaluate(_request(other)))
    assert result.status == EvaluationStatus.UNRESOLVED and result.resolved is False
    assert (result.target_tests.passed, result.target_tests.failed) == (0, 2)
    _no_gold_text(result.model_dump(mode="json"))


@pytest.mark.parametrize(
    ("mode", "status", "reason"),
    [
        ("apply_fail", EvaluationStatus.UNRESOLVED, "PATCH_APPLY_FAILED"),
        ("tests_timeout", EvaluationStatus.TIMEOUT, "TESTS_TIMED_OUT"),
        ("crash", EvaluationStatus.ERROR, "HARNESS_FAILED"),
    ],
)
def test_harness_failures_are_classified(
    tmp_path: Path, harness_mode: Callable[[str], None], mode: str, status: str, reason: str
) -> None:
    harness_mode(mode)
    result = asyncio.run(_evaluator(tmp_path).evaluate(_request()))
    assert (result.status, result.failure_reason) == (status, reason)
    assert result.resolved is not True
    _no_gold_text(result.model_dump(mode="json"))


def test_harness_timeout_kills_the_process(
    tmp_path: Path, harness_mode: Callable[[str], None]
) -> None:
    harness_mode("hang")
    started = time.monotonic()
    result = asyncio.run(_evaluator(tmp_path, timeout=2).evaluate(_request()))
    assert time.monotonic() - started < 30
    assert (result.status, result.failure_reason) == ("TIMEOUT", "HARNESS_TIMED_OUT")
    assert result.harness.exit_code is None
    _assert_process_gone(int((_run_dir(tmp_path) / "harness.pid").read_text()))


def _assert_process_gone(pid: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"harness process {pid} still running")


def test_missing_docker_is_unavailable_but_keeps_localization(
    tmp_path: Path, harness_mode: Callable[[str], None]
) -> None:
    evaluator = _evaluator(tmp_path, preflight=lambda: "Docker is not installed.")
    result = asyncio.run(evaluator.evaluate(_request()))
    assert (result.status, result.failure_reason) == ("UNAVAILABLE", "DOCKER_UNAVAILABLE")
    assert result.resolved is None and result.localization is not None
    assert not (tmp_path / "evaluations" / "runs").exists()  # harness never started


def test_reference_failures_are_errors(tmp_path: Path) -> None:
    def offline(*_: str) -> dict[str, Any]:
        from app.data.source import DatasetFetchError

        raise DatasetFetchError("network down")

    result = asyncio.run(_evaluator(tmp_path, fetch=offline).evaluate(_request()))
    assert (result.status, result.failure_reason) == ("ERROR", "REFERENCE_UNAVAILABLE")

    mismatch = _evaluator(tmp_path / "m", _reference_row(base_commit="f" * 40))
    result = asyncio.run(mismatch.evaluate(_request()))
    assert (result.status, result.failure_reason) == ("ERROR", "REFERENCE_MISMATCH")
    assert result.localization is None


def test_unapproved_workspace_changes_block_evaluation(
    tmp_path: Path, harness_mode: Callable[[str], None]
) -> None:
    request = _request(changed=("pkg/core.py", "pkg/extra.py"))
    result = asyncio.run(_evaluator(tmp_path).evaluate(request))
    assert (result.status, result.failure_reason) == ("ERROR", "WORKSPACE_UNSAFE")
    assert result.safety.unauthorized_writes == 1
    assert not (tmp_path / "evaluations" / "runs").exists()


# --- authorization ---------------------------------------------------------------------------


def test_build_request_requires_approved_applied_patch(penv: dict[str, Any]) -> None:
    with TestClient(_app(penv, _backend(penv))) as client:
        inv, _ = _awaiting(client)
        store = _store(client)
        with pytest.raises(EvaluationRefusedError, match="not EVALUATING"):
            build_request(store.get(inv), [])
        # Force EVALUATING without an approval: still refused.
        store.transition(inv, S.CANCELLED)
    stored = store.get(inv).model_copy(update={"status": S.EVALUATING})
    with pytest.raises(EvaluationRefusedError, match="no approval"):
        build_request(stored, [])


# --- service + API ----------------------------------------------------------------------------


def _evaluated(client: TestClient, inv: str) -> dict:
    assert _decide(client, inv, "approve").status_code == 200
    return _wait_for(client, inv, TERMINAL_STATUSES)


def test_approved_patch_moves_through_evaluating_to_resolved(penv: dict[str, Any]) -> None:
    evaluator = StaticEvaluator(delay=0.5)
    with TestClient(_app(penv, _backend(penv), evaluator)) as client:
        inv, awaiting = _awaiting(client)
        assert _decide(client, inv, "approve").status_code == 200
        evaluating = _wait_for(client, inv, frozenset({S.EVALUATING}) | TERMINAL_STATUSES)
        assert evaluating["status"] == "EVALUATING"
        assert "EVALUATING PATCH" in client.get(f"/investigations/{inv}").text
        body = _wait_for(client, inv, TERMINAL_STATUSES)

    assert body["status"] == "RESOLVED"
    evaluation = body["evaluation"]
    assert evaluation["status"] == "RESOLVED" and evaluation["resolved"] is True
    assert evaluation["trace"]["tool_calls"] == body["tool_call_count"]
    assert evaluation["trace"]["grounded_claims"] == evaluation["trace"]["claims"] > 0
    titles = [e["title"] for e in body["events"]]
    assert titles[-2:] == ["EVALUATION STARTED", "CASE CLOSED"]

    (request,) = evaluator.requests
    assert request.candidate_diff == awaiting["proposed_patch"]["diff"]
    assert request.patch_sha256 == awaiting["proposed_patch"]["sha256"]
    assert request.changed_files == ["pkg/core.py"]
    assert request.suspect_files[0] == "pkg/core.py"
    _assert_no_gold(body)


@pytest.mark.parametrize(
    ("evaluator", "evaluation_status", "title", "page_text"),
    [
        (StaticEvaluator(EvaluationStatus.UNRESOLVED), "UNRESOLVED", "TESTS FAILED",
         "TESTS FAILED"),
        (StaticEvaluator(EvaluationStatus.UNAVAILABLE), "UNAVAILABLE", "EVALUATOR UNAVAILABLE",
         "NOT EVALUATED"),
        (StaticEvaluator(error=RuntimeError("boom GOLD")), "ERROR", "EVALUATION ERROR",
         "EVALUATION ERROR"),
    ],
)  # fmt: skip
def test_unresolved_outcomes_are_persisted(
    penv: dict[str, Any], evaluator: StaticEvaluator, evaluation_status: str, title: str,
    page_text: str,
) -> None:  # fmt: skip
    with TestClient(_app(penv, _backend(penv), evaluator)) as client:
        inv, _ = _awaiting(client)
        body = _evaluated(client, inv)
        page = client.get(f"/investigations/{inv}").text

    assert body["status"] == "UNRESOLVED"
    assert body["evaluation"]["status"] == evaluation_status
    assert body["evaluation"]["resolved"] is not True
    assert body["events"][-1]["title"] == title
    assert page_text in page and "Benchmark evaluation" in page
    assert "GOLD" not in json.dumps(body) and "GOLD" not in page  # exception text not leaked


def test_full_flow_with_harness_subprocess(
    penv: dict[str, Any], harness_mode: Callable[[str], None]
) -> None:
    row = _reference_row(base_commit=penv["case"].base_commit)
    evaluator = _evaluator(penv["tmp"], row)
    with TestClient(_app(penv, _backend(penv), evaluator)) as client:
        inv, _ = _awaiting(client)
        body = _evaluated(client, inv)
        page = client.get(f"/investigations/{inv}").text
        repo = _repo(client, inv)

    assert body["status"] == "RESOLVED", body["evaluation"]
    assert body["evaluation"]["target_tests"] == {"passed": 2, "failed": 0}
    assert body["evaluation"]["localization"]["top1_file_hit"] is True
    assert "CASE CLOSED" in page and "Top-3 file recall" in page
    assert "GOLD" not in page and "helpers_gold" not in page
    _assert_no_gold(body)
    _no_gold_text(body)
    # The investigation workspace is untouched by grading (still just the applied patch).
    assert FIX_LINE + "\n" in (repo / "pkg/core.py").read_text()

    # Agent checkpoints and the agent record never saw reference data or the verdict.
    raw = Path(penv["settings"].checkpoint_path).read_bytes()
    assert b"GOLD" not in raw and b"helpers_gold" not in raw
    with sqlite3.connect(penv["settings"].database_path) as conn:
        (record_json,) = conn.execute(
            "SELECT record_json FROM investigations WHERE investigation_id = ?", (inv,)
        ).fetchone()
    assert "GOLD" not in record_json and "evaluation" not in json.loads(record_json)


def test_extra_workspace_write_blocks_evaluation(
    penv: dict[str, Any], harness_mode: Callable[[str], None]
) -> None:
    evaluator = _evaluator(penv["tmp"], _reference_row(base_commit=penv["case"].base_commit))
    with TestClient(_app(penv, _backend(penv), evaluator)) as client:
        inv, _ = _awaiting(client)
        (_repo(client, inv) / "pkg" / "sneaky.py").write_text("x = 1\n")
        body = _evaluated(client, inv)

    assert body["status"] == "UNRESOLVED"
    assert body["evaluation"]["failure_reason"] == "WORKSPACE_UNSAFE"
    assert body["evaluation"]["safety"]["changed_files"] == ["pkg/core.py", "pkg/sneaky.py"]
    assert body["evaluation"]["safety"]["unauthorized_writes"] == 1


def test_cancel_during_evaluation_stops_the_harness(
    penv: dict[str, Any], harness_mode: Callable[[str], None]
) -> None:
    harness_mode("hang")
    evaluator = _evaluator(penv["tmp"], _reference_row(base_commit=penv["case"].base_commit))
    with TestClient(_app(penv, _backend(penv), evaluator)) as client:
        inv, _ = _awaiting(client)
        _decide(client, inv, "approve")
        pid_file = None
        deadline = time.monotonic() + 30
        while pid_file is None and time.monotonic() < deadline:
            pid_file = next((penv["tmp"] / "evaluations" / "runs").glob("*/*/harness.pid"), None)
            time.sleep(0.05)
        assert pid_file is not None
        assert client.post(f"/api/investigations/{inv}/cancel").status_code == 200
        _assert_process_gone(int(pid_file.read_text()))
        body = client.get(f"/api/investigations/{inv}").json()
    assert body["status"] == "CANCELLED" and body["evaluation"] is None


def test_evaluation_survives_restart(penv: dict[str, Any]) -> None:
    with TestClient(_app(penv, _backend(penv))) as client:
        inv, _ = _awaiting(client)
        before = _evaluated(client, inv)
    with TestClient(_app(penv, _backend(penv))) as client:
        after = client.get(f"/api/investigations/{inv}").json()
    assert after["status"] == "RESOLVED"
    assert after["evaluation"] == before["evaluation"]


def test_restart_during_evaluation_marks_failed(penv: dict[str, Any]) -> None:
    store = InvestigationStore(penv["settings"].database_path)
    inv = str(uuid.uuid4())
    store.create(inv, penv["case"])
    for status in (S.PREPARING, S.INVESTIGATING, S.AWAITING_APPROVAL, S.PATCHING, S.EVALUATING):
        store.transition(inv, status)
    with TestClient(_app(penv, _backend(penv))) as client:
        body = client.get(f"/api/investigations/{inv}").json()
    assert body["status"] == "FAILED" and body["evaluation"] is None


def test_record_evaluation_requires_evaluating(penv: dict[str, Any]) -> None:
    store = InvestigationStore(penv["settings"].database_path)
    inv = str(uuid.uuid4())
    store.create(inv, penv["case"])
    result = asyncio.run(StaticEvaluator().evaluate(_request()))
    event = AgentEvent(type="case_closed", title="x", message="x")
    with pytest.raises(ValueError, match="illegal status transition"):
        store.record_evaluation(inv, result, event=event)
    assert store.get(inv).evaluation is None

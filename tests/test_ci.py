"""CI hygiene: secrets are never required or printed; manifests carry no answer data."""

import json
import re
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.data.store import read_cases
from app.evaluation.report import EvalManifest, load_manifest
from app.workspace.runner import run_command

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
GOLD_KEYS = {"patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS", "hints_text", "eval_script"}
SECRET_ENV_LINE = re.compile(r"^\s+GEMINI_API_KEY: \$\{\{ secrets\.GEMINI_API_KEY \}\}$")
SAFE_EXPRESSION_LINE = re.compile(r"^\s+(if: |name: |group: |[A-Z_]+: )")


def _workflow(name: str) -> list[str]:
    return (WORKFLOWS / name).read_text().splitlines()


def test_normal_ci_uses_no_secrets_and_no_docker_or_model() -> None:
    text = "\n".join(line for line in _workflow("ci.yml") if not line.lstrip().startswith("#"))
    assert "secrets." not in text
    assert "GEMINI" not in text
    assert "docker" not in text.lower() and "eval-integration" not in text
    assert "workflow_dispatch" not in text
    assert "uv sync --locked" in text and "make lint" in text and "make test" in text


def test_expensive_evaluation_is_manual_only() -> None:
    text = (WORKFLOWS / "agent-eval.yml").read_text()
    on_block = text.split("\non:\n", 1)[1].split("\n\n", 1)[0]
    assert "workflow_dispatch" in on_block
    assert "push" not in on_block and "pull_request" not in on_block and "schedule" not in on_block


@pytest.mark.parametrize("name", ["ci.yml", "agent-eval.yml"])
def test_secrets_and_inputs_only_reach_shell_through_env(name: str) -> None:
    for line in _workflow(name):
        if "secrets." in line:
            assert SECRET_ENV_LINE.match(line), line  # step-scoped env only
        if "${{ inputs." in line or "${{ github.event" in line:
            assert SAFE_EXPRESSION_LINE.match(line), line  # never interpolated into `run:`
        assert "set -x" not in line
        assert not re.search(r"echo[^\n]*\$\{?GEMINI_API_KEY", line), line


def test_settings_never_render_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "sk-test-not-a-real-key")
    settings = Settings(_env_file=None)
    assert settings.gemini_api_key is not None
    for rendered in (repr(settings), str(settings), settings.model_dump_json()):
        assert "sk-test-not-a-real-key" not in rendered


def test_tests_run_without_a_model_key() -> None:
    assert Settings(_env_file=None).gemini_api_key is None


def test_agent_eval_refuses_without_key_and_prints_nothing_secret(tmp_path: Path) -> None:
    # cwd has no .env, and the key is empty: the script must stop before any model call.
    result = run_command(
        [sys.executable, str(ROOT / "scripts" / "agent_eval.py"), "--output", "r.json"],
        cwd=tmp_path,
        timeout=60,
        env={"GEMINI_API_KEY": ""},
        check=False,
    )
    assert result.returncode == 3
    assert "GEMINI_API_KEY is not set" in result.stderr
    assert not (tmp_path / "r.json").exists()


def test_live_manifest_is_safe_and_seeded() -> None:
    manifest = load_manifest(ROOT / "evals" / "cases.json")
    raw = json.loads((ROOT / "evals" / "cases.json").read_text())
    for case in raw["cases"]:
        assert not GOLD_KEYS & set(case)
    seeded = {c.instance_id for c in read_cases(ROOT / "data" / "cases.jsonl")}
    if not seeded:
        pytest.skip("data/cases.jsonl not seeded")
    assert {c.instance_id for c in manifest.cases} <= seeded
    assert 1 <= len(manifest.cases) <= 3  # small by design; not the whole benchmark


def test_manifest_rejects_answer_fields() -> None:
    with pytest.raises(ValidationError):
        EvalManifest.model_validate({"cases": [{"instance_id": "a__b-1", "patch": "diff"}]})
    with pytest.raises(ValidationError):
        EvalManifest.model_validate({"cases": [{"instance_id": "../etc"}]})


def test_stub_baseline_is_a_produced_summary() -> None:
    baseline = json.loads((ROOT / "evals" / "baselines" / "stub.json").read_text())
    assert baseline["mode"] == "stub" and baseline["cases"] == []
    assert baseline["summary"]["unauthorized_writes"] == 0
    assert (
        not (ROOT / "evals" / "baselines" / "live.json").exists()
        or json.loads((ROOT / "evals" / "baselines" / "live.json").read_text())["mode"] == "live"
    )

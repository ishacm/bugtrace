"""Gold-data leakage boundary tests (see docs/DATA.md)."""

import ast
import json
import sys
from pathlib import Path
from typing import get_args

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from app.config import Settings, get_settings
from app.data import swebench
from app.data.models import PUBLIC_CASE_FIELDS, PublicCase
from app.data.store import read_cases, write_cases
from app.evaluation.gold import gold_case_from_row
from app.evaluation.models import EvaluationRequest, EvaluationResult
from app.main import create_app
from app.workspace.runner import run_command

ROOT = Path(__file__).resolve().parent.parent
GOLD_KEYS = {
    "patch",
    "test_patch",
    "FAIL_TO_PASS",
    "PASS_TO_PASS",
    "fail_to_pass",
    "pass_to_pass",
    "hints_text",
    "eval_script",
}

RAW_ROW = {
    "repo": "astropy/astropy",
    "instance_id": "astropy__astropy-12907",
    "base_commit": "d16bfe05a744909de4b27f5875fe0d4ed41ce607",
    "problem_statement": "Modeling's `separability_matrix` does not compute correctly.",
    "patch": "diff --git a/x.py b/x.py\n-GOLD\n+GOLD\n",
    "test_patch": "diff --git a/test_x.py b/test_x.py\n+GOLD_TEST\n",
    "hints_text": "the fix is in separable.py",
    "eval_script": "pytest test_x.py",
    "created_at": "2022-03-03T15:14:54Z",
    "version": "4.3",
    "FAIL_TO_PASS": '["test_x.py::test_gold"]',
    "PASS_TO_PASS": '["test_x.py::test_other"]',
    "environment_setup_commit": "298ccb478e6bf092953bca67a3d29dc6c35f6752",
    "difficulty": "15 min - 1 hour",
}


def _assert_no_gold(payload: object) -> None:
    text = json.dumps(payload)
    assert "GOLD" not in text
    assert "separable.py" not in text
    if isinstance(payload, dict):
        assert not GOLD_KEYS & payload.keys()


def test_public_model_has_only_permitted_fields() -> None:
    assert set(PublicCase.model_fields) == PUBLIC_CASE_FIELDS
    assert not GOLD_KEYS & set(PublicCase.model_fields)


@pytest.mark.parametrize("gold_key", sorted(GOLD_KEYS))
def test_public_model_rejects_gold_keys(gold_key: str) -> None:
    data = {k: RAW_ROW[k] for k in ("instance_id", "repo", "problem_statement", "base_commit")}
    with pytest.raises(ValidationError):
        PublicCase.model_validate({**data, gold_key: "leak"})


def test_adapter_strips_gold_fields_from_raw_row() -> None:
    case = swebench.public_case_from_row(RAW_ROW)
    dumped = case.model_dump()
    assert set(dumped) == PUBLIC_CASE_FIELDS
    assert case.issue_url is None
    _assert_no_gold(dumped)


def test_gold_model_lives_in_evaluation_package() -> None:
    gold = gold_case_from_row(RAW_ROW)
    assert gold.fail_to_pass == ["test_x.py::test_gold"]
    assert gold.pass_to_pass == ["test_x.py::test_other"]


def test_seed_output_contains_no_gold_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(swebench, "fetch_raw_rows", lambda *_: [RAW_ROW, RAW_ROW])
    output = tmp_path / "cases.jsonl"
    count = write_cases(output, swebench.load_public_cases("any/dataset", "test", 2))

    lines = output.read_text().splitlines()
    assert count == len(lines) == 2
    for line in lines:
        record = json.loads(line)
        assert set(record) <= PUBLIC_CASE_FIELDS
        _assert_no_gold(record)


def test_seeded_data_file_is_public_only() -> None:
    path = ROOT / "data" / "cases.jsonl"
    if not path.is_file():
        pytest.skip("data/cases.jsonl not seeded")
    for line in path.read_text().splitlines():
        assert set(json.loads(line)) <= PUBLIC_CASE_FIELDS


def test_store_rejects_file_with_leaked_gold_keys(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(RAW_ROW) + "\n")
    with pytest.raises(ValidationError):
        read_cases(path)


def test_cases_api_returns_only_public_data(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    write_cases(path, [swebench.public_case_from_row(RAW_ROW)])
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, cases_path=path)

    response = TestClient(app).get("/api/cases")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert set(body[0]) == PUBLIC_CASE_FIELDS
    assert body[0]["instance_id"] == RAW_ROW["instance_id"]
    _assert_no_gold(body[0])


def test_cases_api_empty_when_not_seeded(tmp_path: Path) -> None:
    app = create_app()
    missing = tmp_path / "missing.jsonl"
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, cases_path=missing)
    assert TestClient(app).get("/api/cases").json() == []


def _imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


# Outside app/evaluation, only the gold-free result models may be imported, plus the
# evaluator entry points by the service layer that runs evaluation after approval.
GOLD_FREE_EVALUATION_MODULES = {"app.evaluation.models"}
EVALUATION_ENTRY_POINTS = {"app.evaluation.evaluator", "app.evaluation.metrics"}
EVALUATION_RUNNERS = {ROOT / "app" / "services" / "investigations.py", ROOT / "app" / "main.py"}


def test_only_evaluation_package_imports_gold_models() -> None:
    offenders = []
    for path in (ROOT / "app").rglob("*.py"):
        if (ROOT / "app" / "evaluation") in path.parents:
            continue
        for module in _imported_modules(path):
            if not module.startswith("app.evaluation") or module in GOLD_FREE_EVALUATION_MODULES:
                continue
            if module in EVALUATION_ENTRY_POINTS and path in EVALUATION_RUNNERS:
                continue
            offenders.append(f"{path.relative_to(ROOT)} -> {module}")
    assert offenders == []


def test_agent_and_tool_layers_never_import_evaluation() -> None:
    offenders = []
    for package in ("agent", "mcp_servers", "workspace", "data"):
        for path in (ROOT / "app" / package).rglob("*.py"):
            if any(m.startswith("app.evaluation") for m in _imported_modules(path)):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_agent_and_tool_processes_never_load_reference_data() -> None:
    """Importing the agent and both MCP servers loads no gold/evaluator code (transitively)."""
    probe = (
        "import sys\n"
        "import app.agent.graph, app.agent.runner\n"
        "import app.mcp_servers.repo_server, app.mcp_servers.patch_server\n"
        "print(sorted(m for m in sys.modules"
        " if m.startswith('app.evaluation') or m == 'app.data.source'))"
    )
    result = run_command([sys.executable, "-c", probe], cwd=ROOT, timeout=60)
    assert result.stdout.strip() == "['app.evaluation', 'app.evaluation.models']"


def test_evaluation_result_models_have_no_gold_fields() -> None:
    def field_names(model: type[BaseModel]) -> set[str]:
        names = set(model.model_fields)
        for field in model.model_fields.values():
            for arg in (field.annotation, *get_args(field.annotation)):
                if isinstance(arg, type) and issubclass(arg, BaseModel):
                    names |= field_names(arg)
        return names

    for model in (EvaluationResult, EvaluationRequest):
        assert not GOLD_KEYS & field_names(model)
    assert "app.evaluation.gold" not in _imported_modules(ROOT / "app" / "evaluation" / "models.py")


def test_agent_layer_does_not_touch_raw_dataset_source() -> None:
    allowed = {ROOT / "app" / "data" / "swebench.py"}
    offenders = []
    for path in (ROOT / "app").rglob("*.py"):
        if path in allowed or (ROOT / "app" / "evaluation") in path.parents:
            continue
        if "app.data.source" in _imported_modules(path):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []

"""TASK 011: model backend selection, Cloud Run configuration and deployment behaviour.

No GCP credentials, network or Docker: Vertex clients resolve credentials lazily and are
never called against the real service here."""

import json
import logging
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.agent.model_client import (
    GeminiModelClient,
    ModelError,
    VertexModelClient,
    build_model_client,
    model_config_problem,
)
from app.agent.models import InvestigationPlan
from app.config import Settings
from app.data.models import PublicCase
from app.data.store import write_cases
from app.evaluation import evaluator as evaluator_module
from app.evaluation.evaluator import DisabledEvaluator, SwebenchEvaluator, build_evaluator
from app.evaluation.models import EvaluationRequest, EvaluationStatus
from app.logs import CloudLoggingFormatter, configure_logging
from app.main import create_app
from app.serve import server_options
from app.services.investigations import InvestigationService, LocalBackend

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import render_cloud_run  # noqa: E402

CLOUD_ENV = ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "PORT", "GOOGLE_GENAI_USE_VERTEXAI")


@pytest.fixture(autouse=True)
def _no_cloud_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in CLOUD_ENV:
        monkeypatch.delenv(name, raising=False)
    for name in ("BUGTRACE_MODEL_BACKEND", "BUGTRACE_MODEL_NAME", "BUGTRACE_PORT"):
        monkeypatch.delenv(name, raising=False)


def _vertex_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUGTRACE_MODEL_BACKEND", "vertex")
    monkeypatch.setenv("BUGTRACE_MODEL_NAME", "configured-model")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "example-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "example-location")


# --- configuration ------------------------------------------------------------------


def test_defaults_are_local_with_no_cloud_identifiers() -> None:
    settings = Settings(_env_file=None)
    assert settings.model_backend == "gemini"
    assert settings.model_name is None
    assert settings.google_cloud_project is None
    assert settings.google_cloud_location is None
    assert settings.evaluation_enabled is True
    assert (settings.host, settings.port, settings.log_format) == ("127.0.0.1", 8000, "text")


def test_vertex_backend_selected_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _vertex_env(monkeypatch)
    settings = Settings(_env_file=None)
    assert settings.model_backend == "vertex"
    assert settings.google_cloud_project == "example-project"
    assert settings.google_cloud_location == "example-location"


def test_unknown_backend_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUGTRACE_MODEL_BACKEND", "openai")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_cloud_run_port_overrides_bugtrace_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUGTRACE_PORT", "9000")
    assert Settings(_env_file=None).port == 9000
    monkeypatch.setenv("PORT", "8080")
    assert Settings(_env_file=None).port == 8080
    monkeypatch.setenv("PORT", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("env", "problem"),
    [
        ({}, "BUGTRACE_MODEL_NAME is not set"),
        ({"BUGTRACE_MODEL_NAME": "m"}, "GEMINI_API_KEY is not set"),
        ({"BUGTRACE_MODEL_NAME": "m", "GEMINI_API_KEY": "k"}, None),
        ({"BUGTRACE_MODEL_BACKEND": "vertex", "BUGTRACE_MODEL_NAME": "m"}, "GOOGLE_CLOUD_PROJECT"),
        (
            {"BUGTRACE_MODEL_BACKEND": "vertex", "BUGTRACE_MODEL_NAME": "m", "GEMINI_API_KEY": "k"},
            "GOOGLE_CLOUD_PROJECT",
        ),
        (
            {
                "BUGTRACE_MODEL_BACKEND": "vertex",
                "BUGTRACE_MODEL_NAME": "m",
                "GOOGLE_CLOUD_PROJECT": "p",
            },
            "GOOGLE_CLOUD_LOCATION",
        ),
        (
            {
                "BUGTRACE_MODEL_BACKEND": "vertex",
                "BUGTRACE_MODEL_NAME": "m",
                "GOOGLE_CLOUD_PROJECT": "p",
                "GOOGLE_CLOUD_LOCATION": "l",
            },
            None,
        ),
    ],
)
def test_model_config_problem(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], problem: str | None
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    settings = Settings(_env_file=None)
    found = model_config_problem(settings)
    if problem is None:
        assert found is None
        build_model_client(settings)
    else:
        assert found is not None and problem in found
        with pytest.raises(ModelError, match=problem):
            build_model_client(settings)


# --- backend selection --------------------------------------------------------------


def test_gemini_backend_builds_api_key_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUGTRACE_MODEL_NAME", "configured-model")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")  # must not flip the backend
    client = build_model_client(Settings(_env_file=None))
    assert isinstance(client, GeminiModelClient)
    assert client.model_name == "configured-model"
    assert client._client.vertexai is False


def test_vertex_backend_builds_adc_client_without_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _vertex_env(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-be-used")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))  # no gcloud ADC either
    client = build_model_client(Settings(_env_file=None))
    assert isinstance(client, VertexModelClient)
    assert client._client.vertexai is True
    assert (client.project, client.location) == ("example-project", "example-location")
    assert client._client._api_client.project == "example-project"
    assert client._client._api_client.location == "example-location"
    assert client.model_name == "configured-model"
    assert not client._client._api_client.api_key


def test_vertex_client_uses_shared_structured_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Unavailable(Exception):
        code = 503

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = VertexModelClient(
        project="p", location="l", model_name="m", timeout_seconds=5, sleep=fake_sleep
    )
    client._api_error = _Unavailable  # type: ignore[attr-defined]
    plan = InvestigationPlan(steps=["a"], search_queries=["b"], symbols=["c"])
    outcomes: list[Any] = [_Unavailable(), plan.model_dump_json()]
    calls: list[dict[str, Any]] = []

    async def fake_generate(**kwargs: Any) -> Any:
        calls.append(kwargs)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return type("Response", (), {"text": outcome})()

    monkeypatch.setattr(client._client.aio.models, "generate_content", fake_generate)

    async def go() -> InvestigationPlan:
        return await client.generate_structured(system="s", prompt="p", schema=InvestigationPlan)

    assert anyio.run(go) == plan
    assert sleeps == [2.0]
    assert calls[-1]["model"] == "m"


def test_local_backend_switches_model_without_agent_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _vertex_env(monkeypatch)
    settings = Settings(_env_file=None, workspaces_dir=tmp_path / "ws")
    assert isinstance(LocalBackend(settings).model(), VertexModelClient)
    # Backend-specific code is confined to the model client module.
    for path in (ROOT / "app" / "agent").glob("*.py"):
        if path.name == "model_client.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"vertex|GeminiModelClient|google\.genai", text, re.I), path


def test_no_hard_coded_cloud_identifiers_in_app() -> None:
    pattern = re.compile(r"(europe|us|asia)-[a-z]+\d|projects/[a-z0-9-]+|gemini-\d")
    for path in (ROOT / "app").rglob("*.py"):
        assert not pattern.search(path.read_text(encoding="utf-8")), path


# --- evaluation stays out of the web service ------------------------------------------


def _request() -> EvaluationRequest:
    return EvaluationRequest(
        investigation_id="00000000-0000-4000-8000-000000000000",
        instance_id="owner__repo-1",
        base_commit="a" * 40,
        patch_id="patch-1",
        patch_sha256="b" * 64,
        candidate_diff="",
        suspect_files=[],
        changed_files=[],
    )


def test_disabled_evaluation_never_loads_reference_data(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_: Any, **__: Any) -> None:
        raise AssertionError("reference data must not be touched")

    monkeypatch.setattr(evaluator_module, "GoldSource", forbidden)
    monkeypatch.setattr(evaluator_module, "docker_preflight", forbidden)
    monkeypatch.setenv("BUGTRACE_EVALUATION_ENABLED", "false")
    evaluator = build_evaluator(Settings(_env_file=None))
    assert isinstance(evaluator, DisabledEvaluator)

    async def go() -> Any:
        return await evaluator.evaluate(_request())

    result = anyio.run(go)
    assert result.status == EvaluationStatus.UNAVAILABLE
    assert result.failure_reason == "EVALUATION_DISABLED"
    assert result.resolved is None and result.localization is None and result.harness is None


def test_evaluation_enabled_by_default_locally(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, evaluation_dir=tmp_path)
    assert isinstance(build_evaluator(settings), SwebenchEvaluator)


# --- readiness ------------------------------------------------------------------------


def _app_settings(tmp_path: Path, *, cases: bool = True) -> Settings:
    cases_path = tmp_path / "cases.jsonl"
    if cases:
        case = PublicCase(
            instance_id="owner__repo-1", repo="owner/repo", problem_statement="x", base_commit="a"
        )
        write_cases(cases_path, [case])
    return Settings(
        _env_file=None,
        cases_path=cases_path,
        database_path=tmp_path / "db" / "bugtrace.db",
        checkpoint_path=tmp_path / "db" / "checkpoints.db",
        workspaces_dir=tmp_path / "workspaces",
        evaluation_enabled=False,
    )


def _checks(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {check["name"]: check for check in body["checks"]}


def test_ready_with_vertex_configuration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _vertex_env(monkeypatch)
    with TestClient(create_app(settings=_app_settings(tmp_path))) as client:
        response = client.get("/api/ready")
        assert client.get("/api/health").json() == {"status": "ok"}
    assert response.status_code == 200, response.text
    checks = _checks(response.json())
    assert response.json()["status"] == "ready"
    assert checks["model"]["detail"] == "vertex"
    assert checks["evaluation"]["detail"] == "disabled in this deployment"


def test_not_ready_without_model_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BUGTRACE_MODEL_BACKEND", "vertex")
    monkeypatch.setenv("BUGTRACE_MODEL_NAME", "configured-model")
    with TestClient(create_app(settings=_app_settings(tmp_path))) as client:
        response = client.get("/api/ready")
        health = client.get("/api/health")
    assert response.status_code == 503
    assert _checks(response.json())["model"] == {
        "name": "model",
        "ok": False,
        "detail": "GOOGLE_CLOUD_PROJECT is not set",
    }
    assert health.status_code == 200  # liveness does not depend on configuration


def test_not_ready_without_cases_and_no_secret_in_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BUGTRACE_MODEL_NAME", "configured-model")
    monkeypatch.setenv("GEMINI_API_KEY", "sk-should-never-appear")
    with TestClient(create_app(settings=_app_settings(tmp_path, cases=False))) as client:
        response = client.get("/api/ready")
    assert response.status_code == 503
    assert _checks(response.json())["cases"]["ok"] is False
    assert _checks(response.json())["model"]["ok"] is True
    assert "sk-should-never-appear" not in response.text


def test_service_uses_disabled_evaluator_when_configured(tmp_path: Path) -> None:
    service = InvestigationService.from_settings(_app_settings(tmp_path))
    assert isinstance(service.evaluator, DisabledEvaluator)


# --- server entrypoint and logs -------------------------------------------------------


def test_server_binds_cloud_run_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "8080")
    monkeypatch.setenv("BUGTRACE_HOST", "0.0.0.0")  # noqa: S104
    options = server_options(Settings(_env_file=None))
    assert options["host"] == "0.0.0.0"  # noqa: S104
    assert options["port"] == 8080
    assert options["log_config"] is None
    assert options["timeout_graceful_shutdown"] < 10  # Cloud Run's SIGTERM grace period


def test_json_log_lines_are_cloud_logging_structured() -> None:
    formatter = CloudLoggingFormatter()
    record = logging.LogRecord("bugtrace.test", logging.WARNING, __file__, 1, "hi %s", ("x",), None)
    payload = json.loads(formatter.format(record))
    assert payload["severity"] == "WARNING"
    assert payload["message"] == "hi x"
    assert payload["logger"] == "bugtrace.test"
    assert datetime.fromisoformat(payload["time"]).tzinfo == UTC
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        record = logging.LogRecord(
            "bugtrace.test", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
        )
    line = formatter.format(record)
    assert "\n" not in line
    assert "RuntimeError: boom" in json.loads(line)["stack_trace"]


def test_configure_logging_json(monkeypatch: pytest.MonkeyPatch) -> None:
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    try:
        monkeypatch.setenv("BUGTRACE_LOG_FORMAT", "json")
        configure_logging(Settings(_env_file=None))
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, CloudLoggingFormatter)
        assert logging.getLogger("uvicorn.access").propagate is True
    finally:
        root.handlers[:], level = saved
        root.setLevel(level)


# --- container image ------------------------------------------------------------------


def _dockerfile_env() -> dict[str, str]:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8").replace("\\\n", " ")
    env: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("ENV "):
            env.update(dict(re.findall(r"(\w+)=(\S+)", line)))
    return env


def test_dockerfile_is_production_safe() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert 'CMD ["python", "-m", "app.serve"]' in text  # exec form, no shell
    assert re.search(r"^USER bugtrace$", text, re.M)
    assert re.search(r"ghcr\.io/astral-sh/uv:\d+\.\d+\.\d+ ", text)  # pinned uv
    assert "uv sync --locked --no-dev" in text
    assert re.search(r"^FROM python:3\.12-", text, re.M)
    assert not re.search(r"KEY|SECRET|TOKEN|CREDENTIAL|PASSWORD", text.split("ENV", 1)[1])
    assert "COPY . " not in text and ".env" not in text


def test_dockerfile_env_produces_cloud_run_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _dockerfile_env()
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    settings = Settings(_env_file=None)
    assert settings.env == "production"
    assert (settings.host, settings.port) == ("0.0.0.0", 8080)  # noqa: S104
    assert settings.log_format == "json"
    assert settings.evaluation_enabled is False
    for path in (
        settings.database_path,
        settings.checkpoint_path,
        settings.workspaces_dir,
        settings.investigations_dir,
        settings.evaluation_dir,
    ):
        assert path.is_absolute() and path.is_relative_to("/tmp/bugtrace"), path  # noqa: S108
    assert isinstance(build_evaluator(settings), DisabledEvaluator)
    assert settings.model_name is None and settings.google_cloud_project is None


def test_dockerignore_excludes_secrets_and_local_state() -> None:
    entries = set((ROOT / ".dockerignore").read_text(encoding="utf-8").split())
    assert {".env", ".env.*", ".git", ".venv", "var", "tests", "*.db"} <= entries
    assert "data" not in entries  # the seeded public cases ship in the image


# --- Cloud Run service config ---------------------------------------------------------

DEPLOY_ENV = {
    "BUGTRACE_SERVICE": "bugtrace",
    "BUGTRACE_IMAGE": "example-docker.pkg.dev/proj/repo/bugtrace:abc123",
    "BUGTRACE_SERVICE_ACCOUNT": "bugtrace-run@proj.iam.gserviceaccount.com",
    "BUGTRACE_MODEL_NAME": "configured-model",
    "GOOGLE_CLOUD_PROJECT": "proj",
    "GOOGLE_CLOUD_LOCATION": "example-location",
}


def test_cloud_run_template_renders() -> None:
    rendered = render_cloud_run.render(render_cloud_run.TEMPLATE.read_text(), DEPLOY_ENV)
    assert "${" not in rendered
    for value in DEPLOY_ENV.values():
        assert value in rendered
    assert 'autoscaling.knative.dev/maxScale: "1"' in rendered
    assert 'run.googleapis.com/cpu-throttling: "false"' in rendered
    assert re.search(r"startupProbe:\s+httpGet:\s+path: /api/ready", rendered)
    assert re.search(r"livenessProbe:\s+httpGet:\s+path: /api/health", rendered)
    assert re.search(r"name: BUGTRACE_EVALUATION_ENABLED\s+value: \"false\"", rendered)
    assert "name: PORT" not in rendered  # reserved; Cloud Run sets it
    assert not re.search(r"GEMINI_API_KEY|secretKeyRef|KEY|PASSWORD", rendered)


@pytest.mark.parametrize(
    "override",
    [
        {"GOOGLE_CLOUD_PROJECT": ""},
        {"BUGTRACE_MODEL_NAME": 'x"\n  injected: true'},
        {"BUGTRACE_IMAGE": "img with space"},
        {"BUGTRACE_SERVICE": "${OTHER}"},
    ],
)
def test_cloud_run_template_rejects_missing_or_unsafe_values(override: dict[str, str]) -> None:
    with pytest.raises(render_cloud_run.RenderError):
        render_cloud_run.render(render_cloud_run.TEMPLATE.read_text(), {**DEPLOY_ENV, **override})


def test_render_script_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in render_cloud_run.REQUIRED:
        monkeypatch.delenv(name, raising=False)
    assert render_cloud_run.main(["render", str(tmp_path / "out.yaml")]) == 2
    for name, value in DEPLOY_ENV.items():
        monkeypatch.setenv(name, value)
    assert render_cloud_run.main(["render", str(tmp_path / "out.yaml")]) == 0
    assert "configured-model" in (tmp_path / "out.yaml").read_text()


def test_gcloudignore_keeps_secrets_out_of_cloud_build_upload() -> None:
    lines = (ROOT / ".gcloudignore").read_text(encoding="utf-8").splitlines()
    assert {".env", ".env.*", ".git", ".venv", "var"} <= set(lines)
    assert "#!include:.gitignore" in lines
    assert "Dockerfile" not in lines  # Cloud Build needs it

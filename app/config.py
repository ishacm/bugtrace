import os
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Only /tmp is writable in a Vercel Function, and nothing there outlives the instance.
VERCEL_SCRATCH = Path("/tmp/bugtrace")  # noqa: S108


class ConfigurationError(RuntimeError):
    """Invalid deployment configuration. Messages name variables, never their values.

    Not a ValueError, so pydantic re-raises it as-is instead of wrapping it in a
    ValidationError that would echo the raw input (which can include DATABASE_URL)."""


def _detect_platform() -> str:
    # Vercel sets VERCEL=1 in every deployment; BUGTRACE_PLATFORM overrides it.
    return "vercel" if os.environ.get("VERCEL") else "local"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BUGTRACE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["development", "test", "production"] = "development"
    # local: the full-featured mode (background runs, SQLite, local evaluation).
    # vercel: serverless public demo (stepwise runs, Postgres, evaluation disabled).
    platform: Literal["local", "vercel"] = Field(default_factory=_detect_platform)
    # background: one in-process asyncio task per run (local, Cloud Run).
    # stepwise: no background work; each POST .../advance runs a bounded slice of the
    # graph and persists it, so any process can continue the run (Vercel).
    execution_mode: Literal["background", "stepwise"] = "background"
    storage_backend: Literal["sqlite", "postgres"] = "sqlite"
    # Postgres connection URL; Vercel's Postgres integrations inject DATABASE_URL/POSTGRES_URL.
    database_url: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "BUGTRACE_DATABASE_URL",
            "DATABASE_URL_DATABASE_URL",  # Vercel Neon integration with DATABASE_URL prefix
            "DATABASE_URL",
            "POSTGRES_URL",
        ),
    )
    log_level: str = "INFO"
    log_format: Literal["text", "json"] = "text"  # json: one structured line per record (Cloud Run)

    # Hybrid deployment support: Vercel renders the UI while the browser sends API
    # requests to the Cloud Run backend. Empty keeps same-origin behavior for local
    # development and all-in-one deployments.
    api_base_url: str = ""
    frontend_origin: str | None = None
    data_dir: Path = Path("var")
    host: str = "127.0.0.1"
    # Cloud Run injects PORT; it wins over BUGTRACE_PORT.
    port: int = Field(
        default=8000, ge=1, le=65535, validation_alias=AliasChoices("PORT", "BUGTRACE_PORT")
    )

    swebench_dataset: str = "princeton-nlp/SWE-bench_Verified"
    swebench_split: str = "test"
    seed_limit: int = Field(default=10, ge=1, le=500)
    cases_path: Path = Path("data/cases.jsonl")

    workspaces_dir: Path = Path("var/workspaces")
    git_timeout_seconds: float = Field(default=600, gt=0)

    # gemini: Gemini Developer API (GEMINI_API_KEY). vertex: Vertex AI via Application
    # Default Credentials (GOOGLE_CLOUD_PROJECT + GOOGLE_CLOUD_LOCATION). No defaults for either.
    model_backend: Literal["gemini", "vertex"] = "gemini"
    model_name: str | None = None
    model_timeout_seconds: float = Field(default=120, gt=0)
    model_max_attempts: int = Field(default=4, ge=1, le=6)  # retries on 429/5xx

    agent_max_iterations: int = Field(default=4, ge=1, le=10)
    agent_max_tool_calls: int = Field(default=30, ge=1, le=200)
    agent_max_patch_attempts: int = Field(default=2, ge=1, le=5)
    investigations_dir: Path = Path("var/investigations")
    database_path: Path = Path("var/bugtrace.db")
    checkpoint_path: Path = Path("var/checkpoints.db")  # LangGraph approval-pause checkpoints
    stream_poll_seconds: float = Field(default=0.5, gt=0, le=10)

    # Stepwise execution. One advance starts graph nodes until `step_budget_seconds`
    # have passed, and is cut off after `step_timeout_seconds` (the interrupted node
    # re-runs from the last checkpoint on the next advance). The lease keeps two
    # requests from driving one run at once and must outlast the platform's own
    # function timeout (300s on Vercel Hobby; see vercel.json).
    step_budget_seconds: float = Field(default=40, gt=0, le=600)
    step_timeout_seconds: float = Field(default=240, gt=0, le=3600)
    step_lease_seconds: float = Field(default=310, gt=0, le=7200)
    step_max_failures: int = Field(default=3, ge=1, le=10)
    poll_interval_seconds: float = Field(default=1.5, gt=0, le=10)  # browser polling

    # SWE-bench evaluation (evaluator-only; never visible to the agent). The evaluator
    # dataset carries the harness fields (image, eval_script, log_parser) that the
    # seed dataset lacks; instance ids and base commits are the same.
    # False on the Cloud Run web service: no Docker there, so evaluation never starts and
    # reference data is never loaded. Grading runs locally or in GitHub Actions instead.
    evaluation_enabled: bool = True
    evaluation_dir: Path = Path("var/evaluations")
    evaluation_dataset: str = "SWE-bench/SWE-bench_Verified"
    evaluation_split: str = "test"
    evaluation_harness_version: str = "5.0.2"  # pinned swebench package
    evaluation_python: str = "3.12"
    evaluation_timeout_seconds: float = Field(default=3600, gt=0)  # whole harness run
    evaluation_test_timeout_seconds: int = Field(default=1800, gt=0)  # tests in the container
    gemini_api_key: SecretStr | None = Field(default=None, validation_alias="GEMINI_API_KEY")
    google_cloud_project: str | None = Field(default=None, validation_alias="GOOGLE_CLOUD_PROJECT")
    google_cloud_location: str | None = Field(
        default=None, validation_alias="GOOGLE_CLOUD_LOCATION"
    )

    @model_validator(mode="after")
    def _apply_platform(self) -> Self:
        explicit = self.model_fields_set
        if self.platform == "vercel":
            self._require_default_or(
                explicit, "execution_mode", "stepwise", "BUGTRACE_EXECUTION_MODE"
            )
            self._require_default_or(
                explicit, "storage_backend", "postgres", "BUGTRACE_STORAGE_BACKEND"
            )
            if self.evaluation_enabled and "evaluation_enabled" in explicit:
                raise ConfigurationError(
                    "BUGTRACE_EVALUATION_ENABLED=true is not supported on Vercel: "
                    "SWE-bench evaluation needs Docker and runs locally or manually only"
                )
            self.evaluation_enabled = False
            # Bounded per-request work: one model call must fit well inside a step.
            defaults: dict[str, object] = {
                "model_timeout_seconds": 45.0,
                "model_max_attempts": 2,
                "git_timeout_seconds": 120.0,
                "data_dir": VERCEL_SCRATCH,
                "workspaces_dir": VERCEL_SCRATCH / "workspaces",
                "investigations_dir": VERCEL_SCRATCH / "investigations",
                "evaluation_dir": VERCEL_SCRATCH / "evaluations",
                "database_path": VERCEL_SCRATCH / "unused.db",
                "checkpoint_path": VERCEL_SCRATCH / "unused-checkpoints.db",
            }
            for name, value in defaults.items():
                if name not in explicit:
                    setattr(self, name, value)
        if self.storage_backend == "postgres":
            url = self.database_url.get_secret_value() if self.database_url else ""
            if not url:
                raise ConfigurationError(
                    "Postgres storage needs DATABASE_URL (or BUGTRACE_DATABASE_URL / POSTGRES_URL)"
                )
            if not url.startswith(("postgres://", "postgresql://")):
                raise ConfigurationError("DATABASE_URL must be a postgres:// or postgresql:// URL")
        if self.step_lease_seconds <= self.step_timeout_seconds:
            raise ConfigurationError(
                "BUGTRACE_STEP_LEASE_SECONDS must be longer than BUGTRACE_STEP_TIMEOUT_SECONDS"
            )
        return self

    def _require_default_or(self, explicit: set[str], name: str, value: str, env: str) -> None:
        if name in explicit and getattr(self, name) != value:
            raise ConfigurationError(f"{env}={getattr(self, name)} is not supported on Vercel")
        setattr(self, name, value)


@lru_cache
def get_settings() -> Settings:
    return Settings()

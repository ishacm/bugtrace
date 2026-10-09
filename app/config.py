from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BUGTRACE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    data_dir: Path = Path("var")
    host: str = "127.0.0.1"
    port: int = 8000

    swebench_dataset: str = "princeton-nlp/SWE-bench_Verified"
    swebench_split: str = "test"
    seed_limit: int = Field(default=10, ge=1, le=500)
    cases_path: Path = Path("data/cases.jsonl")

    workspaces_dir: Path = Path("var/workspaces")
    git_timeout_seconds: float = Field(default=600, gt=0)

    model_backend: Literal["gemini"] = "gemini"
    model_name: str | None = None
    model_timeout_seconds: float = Field(default=120, gt=0)

    agent_max_iterations: int = Field(default=4, ge=1, le=10)
    agent_max_tool_calls: int = Field(default=30, ge=1, le=200)
    agent_max_patch_attempts: int = Field(default=2, ge=1, le=5)
    investigations_dir: Path = Path("var/investigations")
    database_path: Path = Path("var/bugtrace.db")
    checkpoint_path: Path = Path("var/checkpoints.db")  # LangGraph approval-pause checkpoints
    stream_poll_seconds: float = Field(default=0.5, gt=0, le=10)

    # SWE-bench evaluation (evaluator-only; never visible to the agent). The evaluator
    # dataset carries the harness fields (image, eval_script, log_parser) that the
    # seed dataset lacks; instance ids and base commits are the same.
    evaluation_dir: Path = Path("var/evaluations")
    evaluation_dataset: str = "SWE-bench/SWE-bench_Verified"
    evaluation_split: str = "test"
    evaluation_harness_version: str = "5.0.2"  # pinned swebench package
    evaluation_python: str = "3.12"
    evaluation_timeout_seconds: float = Field(default=3600, gt=0)  # whole harness run
    evaluation_test_timeout_seconds: int = Field(default=1800, gt=0)  # tests in the container
    gemini_api_key: SecretStr | None = Field(default=None, validation_alias="GEMINI_API_KEY")


@lru_cache
def get_settings() -> Settings:
    return Settings()

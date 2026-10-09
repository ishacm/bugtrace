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
    investigations_dir: Path = Path("var/investigations")
    database_path: Path = Path("var/bugtrace.db")
    gemini_api_key: SecretStr | None = Field(default=None, validation_alias="GEMINI_API_KEY")


@lru_cache
def get_settings() -> Settings:
    return Settings()

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

    model_name: str | None = None
    gemini_api_key: SecretStr | None = Field(default=None, validation_alias="GEMINI_API_KEY")


@lru_cache
def get_settings() -> Settings:
    return Settings()

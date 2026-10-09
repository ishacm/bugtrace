import pytest

from app.config import Settings


def test_config_loads_in_test_mode_without_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("BUGTRACE_ENV", "test")
    settings = Settings(_env_file=None)
    assert settings.env == "test"
    assert settings.gemini_api_key is None
    assert settings.model_name is None

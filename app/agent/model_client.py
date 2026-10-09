"""Model backend abstraction. Agent code depends only on `ModelClient`."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from pydantic import BaseModel, ValidationError

from app.config import Settings

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class ModelError(RuntimeError):
    pass


class ModelClient(Protocol):
    async def generate_structured[T: BaseModel](
        self, *, system: str, prompt: str, schema: type[T]
    ) -> T: ...


class GeminiModelClient:
    """Gemini API (Google Gen AI SDK) backend with JSON-schema structured output."""

    def __init__(
        self,
        *,
        api_key: str,
        model_name: str,
        timeout_seconds: float,
        max_attempts: int = 4,
        backoff_seconds: float = 2.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        from google import genai
        from google.genai import errors, types

        self._types = types
        self._api_error = errors.APIError
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=int(timeout_seconds * 1000)),
        )
        self.model_name = model_name

    async def generate_structured[T: BaseModel](
        self, *, system: str, prompt: str, schema: type[T]
    ) -> T:
        config = self._types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=schema.model_json_schema(),
            temperature=0.2,
        )
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.aio.models.generate_content(
                    model=self.model_name, contents=prompt, config=config
                )
                break
            except self._api_error as exc:
                # Transient overload/rate-limit: bounded exponential backoff.
                if exc.code not in RETRYABLE_STATUS_CODES or attempt == self._max_attempts:
                    raise ModelError(
                        f"{type(exc).__name__} {exc.code} after {attempt} attempt(s)"
                    ) from exc
                await self._sleep(self._backoff_seconds * 2 ** (attempt - 1))
            except Exception as exc:  # transport errors etc.
                raise ModelError(f"{type(exc).__name__}: {exc}") from exc
        try:
            return schema.model_validate_json(response.text or "")
        except ValidationError as exc:
            raise ModelError(f"model output did not match {schema.__name__}: {exc}") from exc


def build_model_client(settings: Settings) -> ModelClient:
    if settings.model_backend == "gemini":
        if settings.gemini_api_key is None:
            raise ModelError("GEMINI_API_KEY is not set")
        if not settings.model_name:
            raise ModelError("BUGTRACE_MODEL_NAME is not set")
        return GeminiModelClient(
            api_key=settings.gemini_api_key.get_secret_value(),
            model_name=settings.model_name,
            timeout_seconds=settings.model_timeout_seconds,
        )
    raise ModelError(f"unsupported model backend: {settings.model_backend}")

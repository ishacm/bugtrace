"""Model backend abstraction. Agent code depends only on `ModelClient`."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from app.config import Settings

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class ModelError(RuntimeError):
    pass


class ModelClient(Protocol):
    async def generate_structured[T: BaseModel](
        self, *, system: str, prompt: str, schema: type[T]
    ) -> T: ...


class GenAIModelClient:
    """Google Gen AI SDK backend with JSON-schema structured output.

    The Gemini Developer API and Vertex AI differ only in how the SDK client is
    constructed; generation, retries and output validation are shared."""

    def __init__(
        self,
        *,
        client: Any,
        model_name: str,
        max_attempts: int = 4,
        backoff_seconds: float = 2.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        from google.genai import errors, types

        self._types = types
        self._api_error = errors.APIError
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep
        self._client = client
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


def _http_options(timeout_seconds: float) -> Any:
    from google.genai import types

    return types.HttpOptions(timeout=int(timeout_seconds * 1000))


class GeminiModelClient(GenAIModelClient):
    """Gemini Developer API (API key). The local development backend."""

    def __init__(
        self, *, api_key: str, model_name: str, timeout_seconds: float, **kwargs: Any
    ) -> None:
        from google import genai

        client = genai.Client(
            vertexai=False, api_key=api_key, http_options=_http_options(timeout_seconds)
        )
        super().__init__(client=client, model_name=model_name, **kwargs)


class VertexModelClient(GenAIModelClient):
    """Vertex AI. Authenticates with Application Default Credentials (the Cloud Run
    service account in production); credentials are resolved lazily on first call."""

    def __init__(
        self, *, project: str, location: str, model_name: str, timeout_seconds: float, **kwargs: Any
    ) -> None:
        from google import genai

        self.project = project
        self.location = location
        client = genai.Client(
            vertexai=True,
            project=project,
            location=location,
            http_options=_http_options(timeout_seconds),
        )
        super().__init__(client=client, model_name=model_name, **kwargs)


def model_config_problem(settings: Settings) -> str | None:
    """Why the configured backend cannot be built, without contacting any service."""
    if not settings.model_name:
        return "BUGTRACE_MODEL_NAME is not set"
    if settings.model_backend == "gemini":
        if settings.gemini_api_key is None or not settings.gemini_api_key.get_secret_value():
            return "GEMINI_API_KEY is not set"
        return None
    if settings.model_backend == "vertex":
        if not settings.google_cloud_project:
            return "GOOGLE_CLOUD_PROJECT is not set"
        if not settings.google_cloud_location:
            return "GOOGLE_CLOUD_LOCATION is not set"
        return None
    return f"unsupported model backend: {settings.model_backend}"


def build_model_client(settings: Settings) -> ModelClient:
    """The backend is chosen by configuration only; agent code is identical for both."""
    problem = model_config_problem(settings)
    if problem is not None:
        raise ModelError(problem)
    model_name = settings.model_name or ""
    if settings.model_backend == "vertex":
        return VertexModelClient(
            project=settings.google_cloud_project or "",
            location=settings.google_cloud_location or "",
            model_name=model_name,
            timeout_seconds=settings.model_timeout_seconds,
            max_attempts=settings.model_max_attempts,
        )
    api_key = settings.gemini_api_key.get_secret_value() if settings.gemini_api_key else ""
    return GeminiModelClient(
        api_key=api_key,
        model_name=model_name,
        timeout_seconds=settings.model_timeout_seconds,
        max_attempts=settings.model_max_attempts,
    )

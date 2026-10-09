from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from app.agent.model_client import model_config_problem
from app.config import Settings
from app.data.store import read_cases
from app.services.investigations import InvestigationService, LocalBackend

router = APIRouter(prefix="/api")


class HealthResponse(BaseModel):
    status: str


class ReadinessCheck(BaseModel):
    name: str
    ok: bool
    detail: str


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: list[ReadinessCheck]


@router.get("/health")
def health() -> HealthResponse:
    """Liveness: the process is up and serving requests. No dependencies are touched."""
    return HealthResponse(status="ok")


@router.get("/ready")
def ready(request: Request, response: Response) -> ReadinessResponse:
    """Readiness: storage, seeded cases and model configuration are usable.

    Never calls the model or the network; details never contain secret values."""
    settings: Settings = request.app.state.settings
    service: InvestigationService = request.app.state.investigations
    checks = [_storage(service), _cases(service), _model(settings, service)]
    checks.append(
        ReadinessCheck(
            name="evaluation",
            ok=True,
            detail="enabled" if settings.evaluation_enabled else "disabled in this deployment",
        )
    )
    if all(check.ok for check in checks):
        return ReadinessResponse(status="ready", checks=checks)
    response.status_code = 503
    return ReadinessResponse(status="not_ready", checks=checks)


def _storage(service: InvestigationService) -> ReadinessCheck:
    try:
        service.store.list_recent(limit=1)
    except Exception as exc:
        return ReadinessCheck(name="storage", ok=False, detail=type(exc).__name__)
    return ReadinessCheck(name="storage", ok=True, detail="sqlite")


def _cases(service: InvestigationService) -> ReadinessCheck:
    try:
        count = len(read_cases(service.cases_path))
    except Exception as exc:
        return ReadinessCheck(name="cases", ok=False, detail=type(exc).__name__)
    if count == 0:
        return ReadinessCheck(name="cases", ok=False, detail="no seeded cases")
    return ReadinessCheck(name="cases", ok=True, detail=f"{count} cases")


def _model(settings: Settings, service: InvestigationService) -> ReadinessCheck:
    if not isinstance(service.backend, LocalBackend):
        return ReadinessCheck(name="model", ok=True, detail="injected backend")
    problem = model_config_problem(settings)
    if problem is not None:
        return ReadinessCheck(name="model", ok=False, detail=problem)
    return ReadinessCheck(name="model", ok=True, detail=settings.model_backend)

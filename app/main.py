from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.cases import router as cases_router
from app.api.health import router as health_router
from app.api.investigations import router as investigations_router
from app.config import Settings, get_settings
from app.evaluation.boundary import Evaluator
from app.services.investigations import InvestigationBackend, InvestigationService
from app.web.pages import router as pages_router
from app.web.stream import router as stream_router

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(
    *,
    settings: Settings | None = None,
    backend: InvestigationBackend | None = None,
    evaluator: Evaluator | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved = settings or get_settings()
        service = InvestigationService.from_settings(resolved, backend, evaluator)
        service.recover()
        app.state.settings = resolved
        app.state.investigations = service
        yield
        await service.shutdown()

    app = FastAPI(title="BugTrace", version="0.1.0", lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(cases_router)
    app.include_router(investigations_router)
    app.include_router(stream_router)
    app.include_router(pages_router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()

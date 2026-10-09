from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.cases import router as cases_router
from app.api.health import router as health_router
from app.api.investigations import router as investigations_router
from app.config import Settings, get_settings
from app.services.investigations import InvestigationBackend, InvestigationService


def create_app(
    *, settings: Settings | None = None, backend: InvestigationBackend | None = None
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        service = InvestigationService.from_settings(settings or get_settings(), backend)
        service.recover()
        app.state.investigations = service
        yield
        await service.shutdown()

    app = FastAPI(title="BugTrace", version="0.1.0", lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(cases_router)
    app.include_router(investigations_router)
    return app


app = create_app()

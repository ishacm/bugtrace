from fastapi import FastAPI

from app.api.cases import router as cases_router
from app.api.health import router as health_router


def create_app() -> FastAPI:
    app = FastAPI(title="BugTrace", version="0.1.0")
    app.include_router(health_router)
    app.include_router(cases_router)
    return app


app = create_app()

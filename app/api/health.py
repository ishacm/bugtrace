from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api")


class HealthResponse(BaseModel):
    status: str


@router.get("/health")
def health() -> HealthResponse:
    return HealthResponse(status="ok")

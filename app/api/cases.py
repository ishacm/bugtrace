from typing import Annotated

from fastapi import APIRouter, Depends

from app.config import Settings, get_settings
from app.data.models import PublicCase
from app.data.store import read_cases

router = APIRouter(prefix="/api")


@router.get("/cases")
def list_cases(settings: Annotated[Settings, Depends(get_settings)]) -> list[PublicCase]:
    return read_cases(settings.cases_path)

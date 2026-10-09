"""HTML pages: case selection and the live investigation view."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from app.api.investigations import Service
from app.data.store import read_cases
from app.storage.sqlite import InvestigationNotFoundError
from app.web.render import context, render_event, templates

router = APIRouter(include_in_schema=False)


@router.get("/", response_class=HTMLResponse)
def home(request: Request, service: Service) -> HTMLResponse:
    cases = read_cases(service.cases_path) if service.cases_path.exists() else []
    return templates.TemplateResponse(
        request,
        "home.html",
        {"cases": cases, "investigations": service.store.list_recent(limit=25)},
    )


@router.get("/investigations/{investigation_id}", response_class=HTMLResponse)
def investigation_page(investigation_id: UUID, request: Request, service: Service) -> HTMLResponse:
    try:
        stored = service.get(str(investigation_id))
    except InvestigationNotFoundError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"investigation {investigation_id} not found"
        ) from exc
    ctx = context(stored)
    ctx["timeline"] = [render_event(event, seq) for seq, event in enumerate(stored.events)]
    return templates.TemplateResponse(request, "investigation.html", ctx)

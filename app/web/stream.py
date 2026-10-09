"""Server-Sent Events for a single investigation, read from persistence.

The stream is a read-only view over SQLite: it polls the stored investigation and
emits events it has not sent yet. The background run never depends on a client,
so a disconnect only ends this generator. Reconnects resume from `Last-Event-ID`
(the event's sequence number), and every connection starts with a full state
snapshot, so a client can always rebuild the current view.

Messages:
- `timeline` (id = seq): `{"seq", "event", "html"}` for each persisted event.
- `state`: `{"status", "terminal", "updated_at", "panels"}` whenever the state changed.
- `end`: `{"status"}` once the investigation is terminal and fully delivered.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from app.api.investigations import Service
from app.services.investigations import InvestigationService
from app.storage.models import TERMINAL_STATUSES
from app.storage.sqlite import InvestigationNotFoundError
from app.web.render import render_event, render_panels

router = APIRouter(prefix="/api/investigations")

HEARTBEAT_SECONDS = 15.0


def sse(event: str, data: object, *, id: int | None = None) -> str:
    lines = [f"event: {event}"]
    if id is not None:
        lines.append(f"id: {id}")
    lines.append(f"data: {json.dumps(data, separators=(',', ':'))}")
    return "\n".join(lines) + "\n\n"


async def investigation_stream(
    service: InvestigationService,
    investigation_id: str,
    *,
    after: int,
    is_disconnected: Callable[[], Awaitable[bool]],
    poll_seconds: float,
) -> AsyncIterator[str]:
    cursor = after
    last_updated = None
    quiet = 0.0
    yield f"retry: {int(max(poll_seconds, 1) * 1000)}\n\n"
    while not await is_disconnected():
        stored = await asyncio.to_thread(service.get, investigation_id)
        cursor = min(cursor, len(stored.events) - 1)
        new = stored.events[cursor + 1 :]
        for seq, event in enumerate(new, start=cursor + 1):
            payload = {
                "seq": seq,
                "event": event.model_dump(mode="json"),
                "html": render_event(event, seq),
            }
            yield sse("timeline", payload, id=seq)
        cursor += len(new)

        terminal = stored.status in TERMINAL_STATUSES
        if new or stored.updated_at != last_updated:
            last_updated = stored.updated_at
            quiet = 0.0
            yield sse(
                "state",
                {
                    "status": stored.status,
                    "terminal": terminal,
                    "updated_at": stored.updated_at.isoformat(),
                    "panels": render_panels(stored),
                },
            )
        if terminal:
            yield sse("end", {"status": stored.status})
            return

        await asyncio.sleep(poll_seconds)
        quiet += poll_seconds
        if quiet >= HEARTBEAT_SECONDS:
            quiet = 0.0
            yield ": keep-alive\n\n"


def _cursor(after: int | None, last_event_id: str | None) -> int:
    """`Last-Event-ID` (set by EventSource on reconnect) wins over the `after` query."""
    if last_event_id is not None:
        try:
            return max(int(last_event_id), -1)
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "Last-Event-ID must be an integer"
            ) from exc
    return -1 if after is None else after


@router.get("/{investigation_id}/events")
def stream_events(
    investigation_id: UUID,
    request: Request,
    service: Service,
    after: Annotated[int | None, Query(ge=-1)] = None,
    last_event_id: Annotated[str | None, Header(max_length=20)] = None,
) -> StreamingResponse:
    try:
        service.get(str(investigation_id))
    except InvestigationNotFoundError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"investigation {investigation_id} not found"
        ) from exc
    generator = investigation_stream(
        service,
        str(investigation_id),
        after=_cursor(after, last_event_id),
        is_disconnected=request.is_disconnected,
        poll_seconds=request.app.state.settings.stream_poll_seconds,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

# TASK 006 — Investigation API & Persistence

## Read first
- `docs/API.md`
- `docs/ARCHITECTURE.md`
- `docs/AGENT.md`

## Goal
Make investigations durable and controllable through a stable API.

## Implement
- investigation persistence in SQLite.
- event/timeline persistence.
- statuses: CREATED, PREPARING, INVESTIGATING, AWAITING_APPROVAL, PATCHING, EVALUATING, RESOLVED, UNRESOLVED, FAILED, CANCELLED.
- `POST /api/investigations`.
- `GET /api/investigations/{id}`.
- background execution pattern that does not block request for full investigation duration.
- explicit status validation and error handling.
- recovery of stored investigation after server restart.

## Tests
- start investigation.
- fetch state.
- invalid case.
- persistence round trip.
- state transition validation.
- server restart simulation where practical.

## Acceptance criteria
A user can start a real case, poll its status, and retrieve persisted suspects/evidence/root cause.

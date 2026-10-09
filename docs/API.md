# API & UI Contracts

## REST endpoints

### `GET /api/health`
Returns `{ "status": "ok" }`.

### `GET /api/cases`
Returns agent-safe case summaries only.

### `POST /api/investigations`
Request:
```json
{"instance_id":"owner__repo-123"}
```
Response:
```json
{"investigation_id":"<uuid>","status":"PREPARING"}
```

### `GET /api/investigations/{id}`
Returns current public investigation state including status, timeline summary, suspects, evidence, hypothesis, root cause, and V2 patch/evaluation state where available.

### `POST /api/investigations/{id}/decision` (V2)
Request:
```json
{"decision":"approve"}
```
or `reject`.
Return `409` unless status is `AWAITING_APPROVAL`.

### `GET /api/investigations/{id}/events`
Server-Sent Events stream.

## Event types
- `case_started`
- `repo_ready`
- `plan_created`
- `search_started`
- `evidence_found`
- `suspect_found`
- `suspect_removed`
- `hypothesis_created`
- `hypothesis_revised`
- `root_cause_found`
- `patch_proposed`
- `approval_required`
- `evaluation_started`
- `evaluation_completed`
- `case_closed`
- `case_reopened`
- `error`

Every event includes timestamp, type, title, message, and optional metadata.

## UI
Use server-rendered templates and lightweight JavaScript.

### Homepage
Show BugTrace branding, available case cards, repository, shortened problem statement, and Open Case action.

### Investigation page
Show:
- case header/status
- live timeline
- suspect board
- evidence board
- current hypothesis
- root cause
- V2 proposed diff and approval buttons
- V2 evaluation result
- V2 replay control

Never show SWE-bench gold data.

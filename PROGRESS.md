# BugTrace Progress

## Current version
V1

## Current task
TASK 002 — SWE-bench safe data adapter

## Completed
- [x] TASK 001 — Bootstrap
- [ ] TASK 002 — SWE-bench safe data adapter
- [ ] TASK 003 — Repository workspace manager
- [ ] TASK 004 — MCP repository tools
- [ ] TASK 005 — LangGraph investigation agent
- [ ] TASK 006 — API + persistence
- [ ] TASK 007 — Detective UI + live streaming
- [ ] TASK 008 — Patch proposal + human approval
- [ ] TASK 009 — SWE-bench evaluation
- [ ] TASK 010 — Regression evals + CI
- [ ] TASK 011 — Vertex AI + Cloud Run
- [ ] TASK 012 — Polish + optional V3 extension

## Tests
TASK 001: `make test` — 2 passed (health endpoint, config in test mode). `make lint` (ruff check + format check) clean. `make doctor` passes (warnings: `.env` missing, Docker not installed). `make dev` serves `GET /api/health` → `{"status":"ok"}`.

## Blockers
None.

## Known limitations
- Docker not installed locally; doctor reports a WARNING (needed only for V2 evaluation).
- No SWE-bench, LangGraph, MCP or model code yet (by design).

## Notes
Update this file after every task. Do not record fabricated benchmark results.

## Next task
TASK 002 — SWE-bench safe data adapter

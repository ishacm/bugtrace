# BugTrace Progress

## Current version
V1

## Current task
TASK 003 — Repository workspace manager

## Completed
- [x] TASK 001 — Bootstrap
- [x] TASK 002 — SWE-bench safe data adapter
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
TASK 002: `make test` — 20 passed (leakage boundary: PublicCase whitelist + extra=forbid, adapter strips gold, seed output has no gold keys, store rejects leaked keys, `/api/cases` public-only, only `app/evaluation` may import gold models, only the adapter may import raw source). `make lint` clean. `make seed` wrote 10 real SWE-bench Verified cases to `data/cases.jsonl`; `/api/cases` lists 10.

## Blockers
None.

## Known limitations
- Docker not installed locally; doctor reports a WARNING (needed only for V2 evaluation).
- No LangGraph, MCP or model code yet (by design).
- Seed takes the first N rows of the split; the default 10 are all `astropy/astropy`.
- SWE-bench rows have no `issue_url`; field is `null` unless a source provides it.
- `hints_text` is excluded from `PublicCase` (issue comments may hint at the fix).
- Seeding uses the Hugging Face datasets-server REST API (network required).

## Notes
Update this file after every task. Do not record fabricated benchmark results.

## Next task
TASK 003 — Repository workspace manager

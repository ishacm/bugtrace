# TASK 002 — SWE-bench Safe Data Adapter

## Read first
- `docs/DATA.md`
- `docs/API.md` case endpoint section

## Goal
Load a small development set of real SWE-bench Verified cases while making gold-answer leakage structurally impossible in the agent layer.

## Implement
- `PublicCase` Pydantic model with safe fields only.
- evaluator-only `GoldCase` model in evaluation package.
- configurable dataset identifier and case limit.
- dataset adapter that can create `PublicCase` objects.
- `scripts/seed_cases.py` writing safe JSONL under `data/`.
- default seed limit 10.
- `GET /api/cases` returning only public data.
- `make seed` target.

## Important
Do not give agent-facing modules a raw dataset row containing gold keys.

## Tests
- public model has no gold fields.
- seed output contains no `patch`, `test_patch`, `FAIL_TO_PASS`, or `PASS_TO_PASS` keys.
- `/api/cases` returns only public data.

## Acceptance criteria
- 10 real cases can be seeded.
- API lists them.
- leakage tests pass.

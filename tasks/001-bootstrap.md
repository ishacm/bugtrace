# TASK 001 — Project Bootstrap

## Read first
- `docs/PRODUCT.md`
- `docs/ARCHITECTURE.md`

## Goal
Create a clean Python application skeleton that can run locally before adding any AI functionality.

## Implement
- Initialize Git repository if not already initialized.
- Python 3.12 project managed with `uv`.
- FastAPI application.
- `app/config.py` using Pydantic settings.
- `GET /api/health` returning `{ "status": "ok" }`.
- `.env.example` with non-secret placeholders.
- `.gitignore` excluding `.env`, virtual environments, caches, `var/`, local DBs, evaluation artifacts.
- `Makefile` targets: `setup`, `doctor`, `dev`, `test`, `lint`, `format`.
- `scripts/doctor.py` checking Python, Git, writable directories, `.env`, and optionally Docker with a warning rather than V1 failure.
- pytest configuration.
- ruff configuration.
- minimal README placeholder.
- create target directory skeleton from architecture only as needed; avoid empty-file spam.

## Tests
- health endpoint test.
- config loads in test mode without real API keys.

## Acceptance criteria
- `uv sync` succeeds.
- `make doctor` runs and produces understandable PASS/WARNING/FAIL output.
- `make test` passes.
- `make lint` passes.
- `make dev` starts the app.
- No SWE-bench, LangGraph, MCP or model code yet.

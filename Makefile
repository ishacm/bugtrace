.PHONY: setup doctor dev seed workspace investigate test lint format

setup:
	uv sync

doctor:
	uv run python scripts/doctor.py

dev:
	uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

seed:
	uv run python scripts/seed_cases.py

workspace:
	uv run python scripts/prepare_workspace.py $(CASE)

investigate:
	uv run python scripts/investigate.py $(CASE)

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

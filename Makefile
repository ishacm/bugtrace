.PHONY: setup doctor dev seed workspace investigate test regression regression-baseline eval-integration agent-eval lint format

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

# Deterministic end-to-end scenarios (stub model, no Gemini/Docker); writes var/evals/regression.json.
regression:
	REGRESSION_REPORT_PATH=var/evals/regression.json uv run pytest tests/test_regression.py

# After an intended behaviour change: regenerate the stub baseline from a real run.
regression-baseline:
	REGRESSION_REPORT_PATH=var/evals/regression.json uv run pytest tests/test_regression.py -k "not committed_baseline"
	uv run python scripts/promote_baseline.py var/evals/regression.json evals/baselines/stub.json

# Live agent evaluation (needs GEMINI_API_KEY; ARGS=--approve-patches also needs Docker).
agent-eval:
	uv run python scripts/agent_eval.py $(ARGS)

# Real SWE-bench case: needs network; the harness test also needs Docker.
eval-integration:
	SWEBENCH_INTEGRATION=1 uv run pytest -m integration tests/test_evaluation_integration.py

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

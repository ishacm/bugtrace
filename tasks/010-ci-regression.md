# TASK 010 — Regression Evals & GitHub Actions

## Read first
- `docs/EVALUATION.md`

## Goal
Treat agent behavior like software by adding cheap CI and separate controlled AI evaluation.

## Implement
- normal GitHub Actions CI for `uv sync`, `ruff`, unit/integration tests.
- manual `workflow_dispatch` agent-eval workflow.
- small regression case manifest.
- JSON evaluation report.
- compare current metrics with stored baseline when available.
- hard fail on any detected unauthorized write.
- avoid expensive full benchmark runs on ordinary push.

## Acceptance criteria
Normal CI is fast and deterministic. Agent evaluation is explicitly triggered and produces machine-readable artifacts without fabricated metrics.

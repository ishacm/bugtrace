# TASK 009 — SWE-bench Evaluation

## Read first
- `docs/EVALUATION.md`
- `docs/DATA.md`
- `docs/MCP.md`

## Goal
Evaluate approved patches against the real benchmark without leaking the solution into the agent.

## Implement
- evaluator-only gold loader by `instance_id`.
- prediction writer in SWE-bench-compatible format.
- integration wrapper around official SWE-bench evaluation flow where practical.
- evaluation status/log parsing.
- `resolved`/`unresolved` result persistence.
- file localization metrics using gold patch only inside evaluator package.
- efficiency/safety metrics available from execution traces.
- UI display of evaluation result.

## Scope control
Start with 1–3 cases. Do not run the whole Verified dataset.

## Apple Silicon caution
Keep evaluation modular because benchmark containers may be heavy. Do not block the rest of BugTrace if a specific SWE-bench image cannot run locally.

## Tests
- agent state never receives gold fields.
- evaluator can compare predicted suspects to gold-modified files.
- prediction format valid.
- mocked evaluator result persistence.

## Acceptance criteria
At least one supported case can move from approved patch to a recorded benchmark evaluation result, or the repository clearly documents an environment limitation while all integration boundaries are implemented and tested.

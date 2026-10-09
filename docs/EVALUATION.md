# Evaluation Strategy

## Goals
Evaluate both the investigation and the proposed fix. Do not reduce the project to patch pass/fail only.

## Metrics
### File localization
Extract files modified by the gold patch in evaluator-only code. Compare with BugTrace suspects.
Report:
- Top-1 file accuracy
- Top-3 file recall

### Patch resolution
Use official SWE-bench evaluation where practical. Report binary resolved/unresolved per case and aggregate count.

### Evidence grounding
Measure whether root-cause claims include supporting evidence IDs. Prefer a deterministic structural metric over asking an LLM to judge everything.

### Safety
Hard metric:
- unauthorized writes = 0

### Efficiency
Record when available:
- tool calls
- model calls
- duration
- token usage
- cost only if it can be calculated from real provider usage/pricing inputs

## Regression set
Use 5–10 selected development cases. Store only safe identifiers/configuration in the normal repository. Gold data remains evaluator-only.

## CI strategy
Normal CI on every push:
- unit tests
- integration tests using tiny local fixture repo
- ruff

Do not clone large repos or run full SWE-bench evaluation on every push.

Agent evaluation workflow:
- manual `workflow_dispatch`
- optionally scheduled
- run selected cases
- upload JSON report/logs

## Honesty requirement
Never write benchmark results into README until they were actually produced by the current implementation. Keep sample values clearly labeled as examples during development.

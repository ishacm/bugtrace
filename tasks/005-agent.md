# TASK 005 — LangGraph Investigation Agent

## Read first
- `docs/AGENT.md`
- `docs/MCP.md`
- `docs/DATA.md`

## Goal
Build the core evidence-driven V1 investigator.

## Implement
- typed investigation state.
- structured evidence, suspect and hypothesis models.
- model client abstraction with local Gemini API implementation.
- configurable model name.
- explicit LangGraph nodes:
  - understand issue
  - create plan
  - search repository
  - inspect candidates
  - optionally inspect tests/history
  - formulate hypothesis
  - verify evidence sufficiency
  - bounded loop for more evidence
  - produce root cause
  - finalize
- explicit tool-call and iteration counters.
- prompt files rather than large inline prompt strings where practical.

## Rules
- one graph, not multiple agents.
- every factual root-cause claim must reference evidence IDs.
- do not allow patch generation yet.
- if evidence is weak, say so.

## Tests
Use model stubs/fakes for deterministic graph tests where possible.
Test:
- loop bound.
- tool-call bound.
- evidence sufficiency.
- final report contains evidence IDs.
- gold data is absent from state.

## Acceptance criteria
At least one real seeded SWE-bench case can run end-to-end and produce a plausible evidence-backed suspect list and root-cause report.

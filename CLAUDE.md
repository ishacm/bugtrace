# BugTrace — Claude Code Working Instructions

## What this project is
BugTrace is an AI software-incident investigator built on real SWE-bench Verified issues. It investigates a repository, gathers evidence, ranks suspicious files/functions, explains a root cause, proposes a patch, waits for human approval, and evaluates the patch.

## Core stack
- Python 3.12
- uv
- FastAPI
- Pydantic
- LangGraph
- MCP Python SDK
- Google Gen AI SDK
- Git/GitHub
- SQLite in V1/V2
- Docker + SWE-bench evaluator in V2
- GCP Cloud Run + Vertex AI in V3
- Jinja2 + HTML/CSS/vanilla JS for UI

## Non-negotiable architecture rules
1. Use one LangGraph agent. Do not introduce multi-agent architecture.
2. Do not introduce React, Kubernetes, Kafka, or unnecessary microservices.
3. The agent must never receive SWE-bench gold `patch`, `test_patch`, `FAIL_TO_PASS`, `PASS_TO_PASS`, or other answer-leaking metadata.
4. Separate agent-safe case models from evaluator-only gold models.
5. Never expose arbitrary shell execution to the LLM.
6. Never use `shell=True`.
7. Every subprocess call must use an argument list, an explicit working directory, and a timeout.
8. Reject path traversal and access outside the per-investigation repository workspace.
9. Repository mutations require explicit human approval.
10. Never push, merge, or create a remote PR automatically.
11. Keep external model names configurable; do not scatter hard-coded model IDs through the codebase.
12. Every root-cause claim must be backed by evidence IDs returned by tools.
13. Prefer typed Pydantic/TypedDict structures over unstructured dictionaries.
14. Keep the application runnable after every task.
15. Add or update tests for security-sensitive behavior.
16. Never fabricate benchmark or performance numbers.
17. Do not read every document in this repository by default. Read only the current task and the docs that task explicitly references.
18. Do not reread large files after writing them unless debugging or changing them.
19. Keep terminal output concise: prefer `pytest -q`, `git diff --stat`, and tailed logs.
20. Do not proceed to the next numbered task unless the current task's acceptance criteria pass.

## Documentation map
Read only what is relevant to the current task:
- Product scope: `docs/PRODUCT.md`
- Architecture: `docs/ARCHITECTURE.md`
- SWE-bench data and leakage boundary: `docs/DATA.md`
- LangGraph design: `docs/AGENT.md`
- MCP tools and security: `docs/MCP.md`
- FastAPI/UI contracts: `docs/API.md`
- Evaluation: `docs/EVALUATION.md`
- Cloud/V3: `docs/DEPLOYMENT.md`

## Standard workflow for every task
1. Read `CLAUDE.md`.
2. Read `PROGRESS.md`.
3. Read only the current file under `tasks/`.
4. Read only the reference docs named in that task file.
5. Inspect only source files needed for the task.
6. Implement the smallest complete solution satisfying the task.
7. Run the relevant tests and `ruff check .`.
8. Fix failures caused by this task.
9. Update `PROGRESS.md`.
10. Stop. Do not start the next task.

## Completion response format
Be concise. Do not paste whole files. Report only:
- Files changed
- Commands run
- Test/lint result
- Any blocker or limitation
- Suggested local git commit message
- Next task number

## Product language
The UI may use restrained detective language such as:
- CASE OPENED
- PRIMARY SUSPECT
- NEW EVIDENCE
- HYPOTHESIS
- CASE CLOSED
- INVESTIGATION REOPENED
- REPLAY INVESTIGATION

Keep the engineering itself professional.

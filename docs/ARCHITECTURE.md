# Architecture

## High-level architecture

```text
Browser
  |
  v
FastAPI + Jinja/SSE
  |
  v
LangGraph Investigation
  |
  +--> MCP Repo Server --> isolated Git workspace
  |
  +--> ModelClient --> Gemini API (local) / Vertex AI (Cloud Run)
  |
  +--> SQLite persistence (V1/V2)
  |
  +--> MCP Patch Server (approved patch only)
  |
  +--> Evaluator (service-side, outside the agent) --> SWE-bench harness in Docker
```

## Design principles
- One explicit LangGraph graph, not a swarm.
- Read tools are separate from write/evaluation tools.
- Agent-safe case data is separate from evaluator gold data.
- Evidence is structured and referenced by ID.
- Every investigation has an isolated workspace.
- Mutation is approval-gated.
- Expensive evaluation is separate from normal web request handling.

## Core modules
- `app/data/`: SWE-bench adapter and safe models.
- `app/workspace/`: per-investigation Git repositories.
- `app/mcp_servers/`: read-only repository server and the privileged patch server.
- `app/services/`: investigation lifecycle, approval decisions, evaluation hand-off.
- `app/web/`: server-rendered pages and the SSE stream.
- `app/agent/`: state, nodes, prompts and graph.
- `app/api/`: REST endpoints.
- `app/storage/`: persistence.
- `app/evaluation/`: gold metadata and evaluator integration.
- `app/templates/`, `app/static/`: lightweight UI.

## Status lifecycle
As implemented in `app/storage/models.py` (`TRANSITIONS`); the store rejects any other edge.

```text
CREATED -> PREPARING -> INVESTIGATING
INVESTIGATING     -> AWAITING_APPROVAL (COMPLETED report + valid patch) | UNRESOLVED
AWAITING_APPROVAL -> PATCHING (approve) | UNRESOLVED (reject)
PATCHING          -> EVALUATING (applied) | UNRESOLVED (apply refused/failed)
EVALUATING        -> RESOLVED (harness resolved: true) | UNRESOLVED
any non-terminal  -> FAILED | CANCELLED   (AWAITING_APPROVAL cannot fail; it is idle)
```

`EVALUATING -> AWAITING_APPROVAL` (reopen) is a legal edge but no code path uses it yet.

## Constraints
- Maximum investigation iterations: 4 by default.
- Maximum agent tool calls: 30 by default.
- Maximum patch attempts: 2 in V2.
- File reads must be line bounded.
- All subprocesses must have timeouts.

## Frontend decision
Use Jinja2 + HTML/CSS + vanilla JavaScript + Server-Sent Events. The frontend is a demo surface, not the main engineering challenge.

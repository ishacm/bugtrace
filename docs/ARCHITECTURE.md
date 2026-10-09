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
  +--> ModelClient --> Gemini API (local) / Vertex AI (V3)
  |
  +--> SQLite persistence (V1/V2)
  |
  +--> V2 privileged execution --> approved patch --> SWE-bench evaluator
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
- `app/mcp_servers/`: repository and V2 execution tools.
- `app/agent/`: state, nodes, prompts and graph.
- `app/api/`: REST + event streaming.
- `app/storage/`: persistence.
- `app/evaluation/`: gold metadata and evaluator integration.
- `app/templates/`, `app/static/`: lightweight UI.

## Status lifecycle

```text
CREATED
  -> PREPARING
  -> INVESTIGATING
  -> AWAITING_APPROVAL (V2)
  -> PATCHING (V2)
  -> EVALUATING (V2)
  -> RESOLVED | UNRESOLVED | FAILED
```

## Constraints
- Maximum investigation iterations: 4 by default.
- Maximum agent tool calls: 30 by default.
- Maximum patch attempts: 2 in V2.
- File reads must be line bounded.
- All subprocesses must have timeouts.

## Frontend decision
Use Jinja2 + HTML/CSS + vanilla JavaScript + Server-Sent Events. The frontend is a demo surface, not the main engineering challenge.

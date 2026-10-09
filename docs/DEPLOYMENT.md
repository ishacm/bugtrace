# V3 Deployment

## Model abstraction
Create one application-facing `ModelClient` interface. The LangGraph code must not care whether inference uses local Gemini Developer API credentials or Vertex AI.

Configuration examples:

```text
BUGTRACE_MODEL_BACKEND=gemini_api
BUGTRACE_MODEL_NAME=<configured model>
GEMINI_API_KEY=...
```

or:

```text
BUGTRACE_MODEL_BACKEND=vertex
BUGTRACE_MODEL_NAME=<configured model>
GOOGLE_CLOUD_PROJECT=...
GOOGLE_CLOUD_LOCATION=europe-west1
```

Use Google Gen AI SDK. Keep model ID configurable.

## Cloud Run
Deploy the FastAPI/Jinja application to Cloud Run.

Cloud Run web service responsibilities:
- web UI/API
- LangGraph investigation orchestration
- read-oriented repository/tool integration where environment permits
- Vertex AI calls
- persistence integration

Do not attempt Docker-in-Docker SWE-bench evaluation inside the web service.

## Evaluation execution
Keep heavy evaluator execution on a local or GitHub Actions runner initially. The web service may dispatch or reference evaluation jobs rather than running nested containers itself.

## Production persistence
V3 may move from SQLite to PostgreSQL. Add pgvector only if implementing incident memory.

## Optional V3 extension — incident memory
Store embeddings and summaries of prior solved investigations, excluding evaluation leakage. Retrieve similar incidents only as clues; the agent must still inspect the active repository.

## Optional V3 extension — n8n
Use n8n only as an event/business workflow layer:

```text
GitHub event -> n8n -> BugTrace API -> result notification
```

Do not move core reasoning into n8n.

## Optional V3 extension — multimodal
Use a small SWE-bench Multimodal sample if time remains. Treat screenshot observations as structured evidence and then continue normal repository investigation.

# TASK 011 — Vertex AI & Cloud Run

## Read first
- `docs/DEPLOYMENT.md`
- `docs/ARCHITECTURE.md`

## Goal
Move the same application/agent code to a cloud model backend and deploy the web service.

## Implement
- `ModelClient` interface shared by local Gemini API and Vertex AI implementation.
- backend chosen through environment configuration.
- Google Gen AI SDK only; do not add deprecated Vertex generative-model APIs.
- containerize FastAPI app if not already done.
- Cloud Run deployment configuration/instructions.
- application uses Application Default Credentials in cloud mode.
- document required GCP services/roles without storing credentials.

## Important
Do not run Docker-in-Docker SWE-bench evaluation in the Cloud Run web service. Keep heavy evaluation external/local/GitHub Actions.

## Acceptance criteria
- local backend still works.
- Vertex backend can be selected without changing agent code.
- Cloud Run deployment path is reproducible and documented.
- deployed `/api/health` and UI work if user supplies valid GCP configuration.

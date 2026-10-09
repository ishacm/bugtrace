# V3 Deployment

BugTrace has two run modes built from the same code. Nothing about the agent, graph,
tools or prompts changes between them; only configuration does.

| | Local development | Cloud Run (production-style) |
|---|---|---|
| Start | `make dev` (uvicorn `--reload`, 127.0.0.1:8000) | container `CMD python -m app.serve` (0.0.0.0:`$PORT`) |
| Model | `BUGTRACE_MODEL_BACKEND=gemini` + `GEMINI_API_KEY` | `BUGTRACE_MODEL_BACKEND=vertex` + service-account ADC |
| Logs | text | JSON lines (`severity`, `message`, `logger`, `time`, `stack_trace`) |
| SWE-bench evaluation | enabled (needs Docker) | disabled (`BUGTRACE_EVALUATION_ENABLED=false`) |
| State | `var/` on disk | `/tmp/bugtrace` in the instance's in-memory filesystem |

## Model abstraction
`app/agent/model_client.py` holds the only backend-specific code. `ModelClient` is the
interface the agent uses. `GenAIModelClient` implements structured JSON output, bounded
retry and validation once; `GeminiModelClient` (API key) and `VertexModelClient`
(`vertexai=True`, project, location) differ only in how the Google Gen AI SDK client is
built. `build_model_client(settings)` selects one from `BUGTRACE_MODEL_BACKEND`. No
deprecated Vertex generative-model APIs are used. No model ID, project or region is
hard-coded; there are no defaults for them.

Vertex uses Application Default Credentials: the Cloud Run service account in
production, or `gcloud auth application-default login` locally. No key file is needed
or supported in configuration. Credentials are resolved on the first model call, so the
app (and the test suite) starts without them.

To try Vertex locally (no Docker needed):

```bash
gcloud auth application-default login
BUGTRACE_MODEL_BACKEND=vertex BUGTRACE_MODEL_NAME=<vertex-model-id> \
  GOOGLE_CLOUD_PROJECT=<project-id> GOOGLE_CLOUD_LOCATION=<vertex-location> make dev
```

## Environment variables

Required on Cloud Run (set by `deploy/cloud-run.service.yaml`):

| Variable | Meaning |
|---|---|
| `BUGTRACE_MODEL_BACKEND` | `vertex` (`gemini` is the local default) |
| `BUGTRACE_MODEL_NAME` | Gemini model ID available in Vertex AI at `GOOGLE_CLOUD_LOCATION` |
| `GOOGLE_CLOUD_PROJECT` | GCP project ID used for Vertex AI calls |
| `GOOGLE_CLOUD_LOCATION` | Vertex AI location (e.g. a region or `global`), independent of the Cloud Run region |
| `BUGTRACE_EVALUATION_ENABLED` | `false`: evaluation never starts and reference data is never loaded |

Set by the image (`Dockerfile`), override only if needed: `PORT=8080` (Cloud Run sets
it; it takes precedence over `BUGTRACE_PORT`), `BUGTRACE_ENV=production`,
`BUGTRACE_HOST=0.0.0.0`, `BUGTRACE_LOG_FORMAT=json`, and every state path under
`/tmp/bugtrace` (`BUGTRACE_DATABASE_PATH`, `BUGTRACE_CHECKPOINT_PATH`,
`BUGTRACE_WORKSPACES_DIR`, `BUGTRACE_INVESTIGATIONS_DIR`, `BUGTRACE_EVALUATION_DIR`).
`BUGTRACE_CASES_PATH=/app/data/cases.jsonl` (the seeded public cases are baked into the
image; run `make seed` before building to change them).

Used only by the deployment commands below (never by the app): `BUGTRACE_REGION`,
`BUGTRACE_SERVICE`, `BUGTRACE_REPOSITORY`, `BUGTRACE_SERVICE_ACCOUNT`, `BUGTRACE_IMAGE`.

Never set on Cloud Run: `GEMINI_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`. There is no
secret in the image, the service YAML or the repository. `.dockerignore` and
`.gcloudignore` keep `.env`, `var/` and `.git` out of the build context and the Cloud
Build upload.

## Health and readiness
- `GET /api/health` (liveness probe): returns `{"status":"ok"}` while the process serves
  requests. Touches nothing else.
- `GET /api/ready` (startup probe): 200 `ready` or 503 `not_ready` with per-check
  details: `storage` (SQLite query), `cases` (seeded cases readable and non-empty),
  `model` (backend configuration complete; names a missing variable, never a value) and
  `evaluation` (informational). It never calls the model or the network, so it does not
  prove that the service account can reach Vertex AI; the first investigation does.
  A revision with incomplete configuration fails its startup probe and never serves.

## Storage: local-only vs production-safe
Cloud Run's writable filesystem is in-memory, per instance, and discarded when the
instance stops (scale to zero, redeploy, crash). It also counts against the memory limit.

| Data | Where | Production-safe? |
|---|---|---|
| Seeded public cases | image (`/app/data/cases.jsonl`), read-only | Yes |
| Investigations, events, approvals, evaluation results (SQLite) | `/tmp/bugtrace/bugtrace.db` | **No.** Lost with the instance |
| LangGraph approval checkpoints (SQLite) | `/tmp/bugtrace/checkpoints.db` | **No.** A case waiting for approval cannot resume on a new instance |
| Repository workspaces (git clones) | `/tmp/bugtrace/workspaces` | Scratch only; this is acceptable, they are per-investigation |
| Investigation JSON exports (`make investigate`) | `/tmp/bugtrace/investigations` | Not used by the web service |

SQLite is not a production database on Cloud Run, and this task does not add one. The
deployment is a single-instance demo: `maxScale: 1` keeps every request on the instance
that holds the data, and `cpu-throttling: false` keeps background investigations running
between requests. With `minScale: 0` an idle instance shuts down after some minutes and
all history goes with it; set `minScale: "1"` to keep it warm (billed continuously), but
data is still lost on redeploy or instance replacement. Runs in flight at shutdown are
marked FAILED (SIGTERM → graceful shutdown in 8 s, inside Cloud Run's 10 s).

Durable production persistence needs a managed database (PostgreSQL on Cloud SQL) for
the store and a LangGraph Postgres checkpointer. That is future work (TASK 012 or
later). Mounting SQLite on a Cloud Storage FUSE volume is not a safe substitute: it does
not provide the file locking SQLite needs.

## Evaluation stays off the web service
The image contains no Docker and no uv, and `BUGTRACE_EVALUATION_ENABLED=false` makes
the service use `DisabledEvaluator`. An approved patch is still applied in the
investigation's workspace, then the case ends UNRESOLVED with `EVALUATION_DISABLED`
(UI: NOT EVALUATED). The disabled evaluator never constructs the reference (gold) data
source and never starts the harness, so the gold boundary is stricter in the cloud than
locally. Grade patches locally (`make dev` with Docker, `make agent-eval
ARGS=--approve-patches`) or with the manual GitHub Actions workflow
(`.github/workflows/agent-eval.yml`).

## Security notes
- The API and UI have no authentication of their own (V1 scope). The service is
  deployed **without** `allUsers` access: Cloud Run IAM requires an identity token, and
  only principals you grant `roles/run.invoker` can call it. Do not make it public.
- The container runs as a non-root user (`bugtrace`, uid 10001).
- Investigations clone public GitHub repositories over HTTPS, so the service needs
  outbound internet access (Cloud Run's default).

## Cloud Run deployment

Prerequisites: `gcloud` CLI authenticated as a user with, on the project,
`roles/run.admin`, `roles/artifactregistry.admin` (or `.writer` once the repository
exists), `roles/cloudbuild.builds.editor`, `roles/iam.serviceAccountAdmin`,
`roles/resourcemanager.projectIamAdmin` (only to grant the runtime role) and
`roles/serviceusage.serviceUsageAdmin` (only to enable APIs), plus
`roles/iam.serviceAccountUser` on the runtime service account. A project owner has all
of these.

Required APIs: Cloud Run (`run.googleapis.com`), Artifact Registry
(`artifactregistry.googleapis.com`), Cloud Build (`cloudbuild.googleapis.com`), Vertex AI
(`aiplatform.googleapis.com`), IAM (`iam.googleapis.com`).

Runtime service account role: `roles/aiplatform.user` (Vertex AI User). Nothing else.

```bash
# 1. Configuration (fill in your own values)
export GOOGLE_CLOUD_PROJECT=<project-id>
export BUGTRACE_REGION=<cloud-run-region>
export GOOGLE_CLOUD_LOCATION=<vertex-location>
export BUGTRACE_MODEL_NAME=<vertex-model-id>
export BUGTRACE_SERVICE=bugtrace
export BUGTRACE_REPOSITORY=bugtrace
export BUGTRACE_SERVICE_ACCOUNT="bugtrace-run@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com"
export BUGTRACE_IMAGE="${BUGTRACE_REGION}-docker.pkg.dev/${GOOGLE_CLOUD_PROJECT}/${BUGTRACE_REPOSITORY}/bugtrace:$(git rev-parse --short HEAD)"

# 2. One-time project setup
gcloud config set project "$GOOGLE_CLOUD_PROJECT"
gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com aiplatform.googleapis.com iam.googleapis.com
gcloud artifacts repositories create "$BUGTRACE_REPOSITORY" \
  --repository-format=docker --location="$BUGTRACE_REGION"
gcloud iam service-accounts create bugtrace-run --display-name="BugTrace Cloud Run runtime"
gcloud projects add-iam-policy-binding "$GOOGLE_CLOUD_PROJECT" \
  --member="serviceAccount:${BUGTRACE_SERVICE_ACCOUNT}" --role="roles/aiplatform.user"

# 3. Build the image (Cloud Build; no local Docker needed). Commit first so the tag
#    matches the source.
gcloud builds submit --tag "$BUGTRACE_IMAGE" .

# 4. Render the service definition and deploy it
make cloud-run-config            # writes var/deploy/service.yaml from the variables above
gcloud run services replace var/deploy/service.yaml --region "$BUGTRACE_REGION"

# 5. Grant yourself access (never allUsers)
gcloud run services add-iam-policy-binding "$BUGTRACE_SERVICE" --region "$BUGTRACE_REGION" \
  --member="user:<your-account-email>" --role="roles/run.invoker"

# 6. Use it through an authenticated local proxy
gcloud run services proxy "$BUGTRACE_SERVICE" --region "$BUGTRACE_REGION" --port 8080
curl -s localhost:8080/api/health   # {"status":"ok"}
curl -s localhost:8080/api/ready    # {"status":"ready",...}
open http://localhost:8080/         # UI
```

Redeploy after a code change: commit, re-export `BUGTRACE_IMAGE` (new short SHA), then
repeat steps 3–4. Logs: `gcloud run services logs read "$BUGTRACE_SERVICE" --region
"$BUGTRACE_REGION"` or Logs Explorer (JSON lines appear with their severity).

`scripts/render_cloud_run.py` fails if any variable is missing or contains characters
outside `[A-Za-z0-9._:/@-]`, so a value cannot inject YAML.

### Local container check (needs Docker)
```bash
make docker-build
BUGTRACE_MODEL_NAME=<model-id> GEMINI_API_KEY=<key> make docker-run   # gemini backend
curl -s localhost:8080/api/ready
```
For Vertex in the local container, mount ADC yourself (not provided by the make target).

### Validation status
The image build has not been run in this environment (Docker is not installed). What was
validated: `uv sync --locked --no-dev --no-install-project` into a fresh environment, and
`python -m app.serve` from a copy of exactly what the image contains, under `env -i`
with the image's environment and the vertex backend. Results: `/api/health` 200,
`/api/ready` 200 (`ready`), UI and static files 200, JSON logs, clean SIGTERM shutdown.
No deployment to GCP has been made from this repository yet, and no live Vertex AI call
has been made.

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

# BugTrace web service image (Cloud Run). Local development does not use this file.
# Build: docker build -t bugtrace .   Run: see docs/DEPLOYMENT.md.

FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.24 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project

FROM python:3.12-slim-bookworm
# git: per-investigation repository workspaces. No Docker, no uv: evaluation is disabled.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 bugtrace
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY app ./app
COPY data/cases.jsonl ./data/cases.jsonl

# Everything mutable lives under /tmp/bugtrace: Cloud Run's writable filesystem is
# in-memory and per-instance, so it is lost when the instance stops (docs/DEPLOYMENT.md).
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    BUGTRACE_ENV=production \
    BUGTRACE_HOST=0.0.0.0 \
    BUGTRACE_LOG_FORMAT=json \
    BUGTRACE_EVALUATION_ENABLED=false \
    BUGTRACE_DATA_DIR=/tmp/bugtrace \
    BUGTRACE_WORKSPACES_DIR=/tmp/bugtrace/workspaces \
    BUGTRACE_INVESTIGATIONS_DIR=/tmp/bugtrace/investigations \
    BUGTRACE_DATABASE_PATH=/tmp/bugtrace/bugtrace.db \
    BUGTRACE_CHECKPOINT_PATH=/tmp/bugtrace/checkpoints.db \
    BUGTRACE_EVALUATION_DIR=/tmp/bugtrace/evaluations \
    BUGTRACE_CASES_PATH=/app/data/cases.jsonl

USER bugtrace
EXPOSE 8080
CMD ["python", "-m", "app.serve"]

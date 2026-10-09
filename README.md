# BugTrace

An AI detective for real software incidents, built on SWE-bench Verified issues.

> Early development. See `docs/PRODUCT.md` for scope and `PROGRESS.md` for status.

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Git.

```bash
cp .env.example .env
make setup    # uv sync
make doctor   # environment checks
make dev      # http://127.0.0.1:8000/api/health
make test
make lint
```

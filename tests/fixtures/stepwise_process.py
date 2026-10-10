"""One short-lived 'serverless process' for the stepwise regression test.

Builds a fresh service over durable files (SQLite store + checkpoints stand in for
Postgres), handles at most `--steps` advance requests, prints the outcome as JSON
and exits, taking every in-memory object with it. Offline: fixture repo, stub model.
"""

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

from app.config import Settings
from app.evaluation.boundary import DisabledEvaluator
from app.services.investigations import InvestigationService
from tests.test_investigations_api import INSTANCE_ID, FakeBackend


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--origin", type=Path, required=True)
    parser.add_argument("--investigation-id")
    parser.add_argument("--steps", type=int, required=True)
    args = parser.parse_args()
    settings = Settings(
        _env_file=None,
        cases_path=args.root / "cases.jsonl",
        database_path=args.root / "db" / "bugtrace.db",
        checkpoint_path=args.root / "db" / "checkpoints.db",
        workspaces_dir=args.root / "tmp-workspaces",
        execution_mode="stepwise",
        evaluation_enabled=False,
        step_budget_seconds=0.000001,
    )
    # This process starts with an empty /tmp, like a new serverless instance.
    shutil.rmtree(settings.workspaces_dir, ignore_errors=True)
    backend = FakeBackend(
        settings.workspaces_dir, args.origin, database_path=settings.database_path
    )
    service = InvestigationService.from_settings(settings, backend, DisabledEvaluator())
    inv = args.investigation_id or service.start(INSTANCE_ID).investigation_id
    results, pending = [], True
    for _ in range(args.steps):
        outcome = await service.advance(inv)
        results.append(outcome.result)
        pending = outcome.pending
        if not pending:
            break
    stored = service.get(inv)
    print(
        json.dumps(
            {
                "investigation_id": inv,
                "status": stored.status,
                "results": results,
                "pending": pending,
                "events": [e.type for e in stored.events],
                "background_tasks": len(service._tasks),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

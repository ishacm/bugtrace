"""Run a BugTrace investigation for one seeded case (prepares the workspace if needed)."""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.model_client import ModelError, build_model_client  # noqa: E402
from app.agent.runner import run_investigation, save_record  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.data.store import read_cases  # noqa: E402
from app.mcp_servers.client import RepoMCPClient  # noqa: E402
from app.workspace.manager import (  # noqa: E402
    WorkspaceError,
    WorkspaceManager,
    new_investigation_id,
)


async def investigate(instance_id: str | None, investigation_id: str | None) -> int:
    settings = get_settings()
    cases = read_cases(settings.cases_path)
    if not cases:
        print("investigate: no seeded cases; run `make seed` first", file=sys.stderr)
        return 1
    case = (
        next((c for c in cases if c.instance_id == instance_id), None) if instance_id else cases[0]
    )
    if case is None:
        print(f"investigate: unknown instance_id {instance_id!r}", file=sys.stderr)
        return 1
    try:
        model = build_model_client(settings)
    except ModelError as exc:
        print(f"investigate: {exc} (see .env.example)", file=sys.stderr)
        return 1

    workspaces = settings.workspaces_dir.resolve()
    manager = WorkspaceManager(workspaces, git_timeout=settings.git_timeout_seconds)
    investigation_id = investigation_id or new_investigation_id()
    try:
        manager.prepare(investigation_id, case)
    except WorkspaceError as exc:
        print(f"investigate: {exc}", file=sys.stderr)
        return 1

    print(f"CASE OPENED {case.instance_id} investigation={investigation_id}")
    async with RepoMCPClient.connect(investigation_id, workspaces) as client:
        state = await run_investigation(
            case=case,
            investigation_id=investigation_id,
            model=model,
            repo_client=client,
            max_iterations=settings.agent_max_iterations,
            max_tool_calls=settings.agent_max_tool_calls,
        )
    path = save_record(state, settings.investigations_dir)
    report = state["final_report"]
    if report is None:
        print("investigate: graph produced no report", file=sys.stderr)
        return 1
    print(f"status={report.status} confidence={report.confidence} "
          f"tool_calls={report.tool_calls} iterations={report.iterations} "
          f"limits={report.limits_reached or '-'}")  # fmt: skip
    for suspect in report.suspects[:5]:
        print(f"  #{suspect.rank} {suspect.path} {suspect.symbol or ''} score={suspect.score}")
    print(f"ROOT CAUSE: {report.summary}")
    for claim in report.claims:
        print(f"  - {claim.statement} {claim.evidence_ids}")
    for error in state["errors"]:
        print(f"  ! {error}")
    print(f"record: {path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instance_id", nargs="?")
    parser.add_argument("--investigation-id", default=None)
    args = parser.parse_args()
    return asyncio.run(investigate(args.instance_id, args.investigation_id))


if __name__ == "__main__":
    sys.exit(main())

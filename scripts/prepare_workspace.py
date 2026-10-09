"""Manually prepare a workspace for one seeded case at its base commit."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.data.store import read_cases  # noqa: E402
from app.workspace.manager import (  # noqa: E402
    WorkspaceError,
    WorkspaceManager,
    new_investigation_id,
)


def main() -> int:
    settings = get_settings()
    cases = read_cases(settings.cases_path)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instance_id", nargs="?", help="defaults to the first seeded case")
    parser.add_argument("--investigation-id", default=None)
    args = parser.parse_args()

    if not cases:
        print("workspace: no seeded cases; run `make seed` first", file=sys.stderr)
        return 1
    by_id = {case.instance_id: case for case in cases}
    case = by_id.get(args.instance_id) if args.instance_id else cases[0]
    if case is None:
        print(f"workspace: unknown instance_id {args.instance_id!r}", file=sys.stderr)
        return 1

    manager = WorkspaceManager(
        settings.workspaces_dir.resolve(), git_timeout=settings.git_timeout_seconds
    )
    investigation_id = args.investigation_id or new_investigation_id()
    print(f"workspace: preparing {case.instance_id} ({case.repo} @ {case.base_commit[:12]})")
    try:
        workspace = manager.prepare(investigation_id, case)
    except WorkspaceError as exc:
        print(f"workspace: FAILED {exc}", file=sys.stderr)
        return 1
    head = manager.head(investigation_id)
    print(f"workspace: ready {workspace.repo_path}")
    print(f"workspace: HEAD {head} matches base_commit: {head == case.base_commit}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

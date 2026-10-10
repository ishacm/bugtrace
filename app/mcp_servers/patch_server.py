"""Privileged execution MCP server: applies an approved patch inside one workspace.

Run: python -m app.mcp_servers.patch_server --investigation-id <uuid>
Separate from the read-only repo server. Its single tool takes no diff argument
and refuses unless the persisted investigation has an approval for its patch.
"""

import argparse
import os
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from app.config import get_settings
from app.mcp_servers.client import DATABASE_URL_ENV
from app.mcp_servers.models import ApplyPatchResult
from app.mcp_servers.patch_tools import PatchTools
from app.storage.sqlite import InvestigationStore
from app.workspace.manager import WorkspaceError, WorkspaceManager
from app.workspace.runner import CommandError

PRIVILEGED = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)


def build_server(tools: PatchTools) -> MCPServer:
    server = MCPServer(
        "bugtrace-patch",
        instructions="Applies the human-approved patch to this investigation's workspace only.",
    )

    @server.tool(annotations=PRIVILEGED)
    def apply_patch() -> ApplyPatchResult:
        """Apply the approved patch (git apply --check, then git apply). Never commits."""
        return tools.apply_patch()

    return server


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--investigation-id", required=True)
    parser.add_argument("--workspaces-dir", type=Path, default=settings.workspaces_dir)
    parser.add_argument("--database-path", type=Path, default=settings.database_path)
    args = parser.parse_args()

    manager = WorkspaceManager(args.workspaces_dir.resolve())
    try:
        # Postgres (hosted mode) when the parent passed a URL in the environment.
        url = os.environ.get(DATABASE_URL_ENV)
        if url:
            from app.storage.postgres import PostgresInvestigationStore

            store: InvestigationStore = PostgresInvestigationStore(url)
        else:
            store = InvestigationStore(args.database_path.resolve())
        tools = PatchTools.for_investigation(manager, store, args.investigation_id)
    except (WorkspaceError, CommandError, FileNotFoundError) as exc:
        print(f"patch_server: {exc}", file=sys.stderr)
        return 2
    build_server(tools).run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())

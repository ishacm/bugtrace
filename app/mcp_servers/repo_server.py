"""Read-only repository MCP server, bound at launch to one investigation workspace.

Run: python -m app.mcp_servers.repo_server --investigation-id <uuid>
The workspace is fixed by the launcher; tools cannot choose another one.
"""

import argparse
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from app.config import get_settings
from app.mcp_servers.models import (
    DEFAULT_SEARCH_RESULTS,
    CheckPatchResult,
    FindSymbolResult,
    GitLogResult,
    GitTextResult,
    ListDirectoryResult,
    ListTestFilesResult,
    ReadFileResult,
    SearchCodeResult,
)
from app.mcp_servers.repo_tools import RepoTools
from app.workspace.manager import WorkspaceError, WorkspaceManager
from app.workspace.runner import CommandError

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)


def build_server(tools: RepoTools) -> MCPServer:
    server = MCPServer(
        "bugtrace-repo",
        instructions="Read-only tools for one repository checked out at the case base commit.",
    )

    @server.tool(annotations=READ_ONLY)
    def list_directory(path: str = ".") -> ListDirectoryResult:
        """List entries of a repository-relative directory."""
        return tools.list_directory(path)

    @server.tool(annotations=READ_ONLY)
    def search_code(
        query: str,
        glob: str | None = None,
        max_results: int = DEFAULT_SEARCH_RESULTS,
        regex: bool = False,
    ) -> SearchCodeResult:
        """Search tracked files for text (fixed string unless regex=true). Results are capped."""
        return tools.search_code(query, glob, max_results, regex)

    @server.tool(annotations=READ_ONLY)
    def read_file(path: str, start_line: int = 1, end_line: int | None = None) -> ReadFileResult:
        """Read numbered lines from a repository file (at most 300 lines per call)."""
        return tools.read_file(path, start_line, end_line)

    @server.tool(annotations=READ_ONLY)
    def find_symbol(symbol: str, max_results: int = 50) -> FindSymbolResult:
        """Find Python def/class/module-level assignment sites for an identifier."""
        return tools.find_symbol(symbol, max_results)

    @server.tool(annotations=READ_ONLY)
    def git_log(path: str | None = None, max_count: int = 20) -> GitLogResult:
        """Show recent commits, optionally limited to a path."""
        return tools.git_log(path, max_count)

    @server.tool(annotations=READ_ONLY)
    def git_show(commit: str = "HEAD", path: str | None = None) -> GitTextResult:
        """Show a commit's stat and patch, optionally limited to a path."""
        return tools.git_show(commit, path)

    @server.tool(annotations=READ_ONLY)
    def git_diff(
        base: str = "HEAD", head: str | None = None, path: str | None = None
    ) -> GitTextResult:
        """Diff two commits, or a commit against the working tree when head is omitted."""
        return tools.git_diff(base, head, path)

    @server.tool(annotations=READ_ONLY)
    def list_test_files(path: str = ".", max_results: int = 200) -> ListTestFilesResult:
        """List likely test files under a repository-relative path."""
        return tools.list_test_files(path, max_results)

    @server.tool(annotations=READ_ONLY)
    def check_patch(diff: str) -> CheckPatchResult:
        """Validate a unified diff and run `git apply --check`. Never modifies the repository."""
        return tools.check_patch(diff)

    return server


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--investigation-id", required=True)
    parser.add_argument("--workspaces-dir", type=Path, default=settings.workspaces_dir)
    args = parser.parse_args()

    manager = WorkspaceManager(args.workspaces_dir.resolve())
    try:
        tools = RepoTools.for_investigation(manager, args.investigation_id)
    except (WorkspaceError, CommandError, FileNotFoundError) as exc:
        # stdout is the MCP channel; diagnostics go to stderr.
        print(f"repo_server: {exc}", file=sys.stderr)
        return 2
    build_server(tools).run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Thin application-side clients for the repository and patch MCP servers."""

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult

from app.mcp_servers.models import (
    ApplyPatchResult,
    CheckPatchResult,
    FindSymbolResult,
    GitLogResult,
    GitTextResult,
    ListDirectoryResult,
    ListTestFilesResult,
    ReadFileResult,
    SearchCodeResult,
    ToolError,
    ToolErrorCode,
    ToolResponse,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CALL_TIMEOUT_SECONDS = 60.0


@asynccontextmanager
async def _stdio_session(module: str, args: list[str]) -> AsyncIterator[ClientSession]:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", module, *args], cwd=PROJECT_ROOT
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        yield session


class _MCPToolClient:
    def __init__(self, session: ClientSession, call_timeout: float) -> None:
        self._session = session
        self._call_timeout = call_timeout

    async def list_tool_names(self) -> list[str]:
        return [tool.name for tool in (await self._session.list_tools()).tools]

    async def _call[R: ToolResponse](
        self, name: str, arguments: dict[str, Any], result_type: type[R]
    ) -> R:
        args = {k: v for k, v in arguments.items() if v is not None}
        result = await self._session.call_tool(name, args, read_timeout_seconds=self._call_timeout)
        if isinstance(result, CallToolResult) and not result.is_error and result.structured_content:
            return result_type.model_validate(result.structured_content)
        message = "tool call failed"
        if isinstance(result, CallToolResult) and result.content:
            message = getattr(result.content[0], "text", message)
        return result_type(
            ok=False, error=ToolError(code=ToolErrorCode.INVALID_ARGUMENT, message=message)
        )


class RepoMCPClient(_MCPToolClient):
    @classmethod
    @asynccontextmanager
    async def connect(
        cls,
        investigation_id: str,
        workspaces_dir: Path,
        call_timeout: float = DEFAULT_CALL_TIMEOUT_SECONDS,
    ) -> AsyncIterator["RepoMCPClient"]:
        args = [
            "--investigation-id",
            investigation_id,
            "--workspaces-dir",
            str(workspaces_dir.resolve()),
        ]
        async with _stdio_session("app.mcp_servers.repo_server", args) as session:
            yield cls(session, call_timeout)

    async def list_directory(self, path: str = ".") -> ListDirectoryResult:
        return await self._call("list_directory", {"path": path}, ListDirectoryResult)

    async def search_code(
        self, query: str, glob: str | None = None, max_results: int = 20, regex: bool = False
    ) -> SearchCodeResult:
        args = {"query": query, "glob": glob, "max_results": max_results, "regex": regex}
        return await self._call("search_code", args, SearchCodeResult)

    async def read_file(
        self, path: str, start_line: int = 1, end_line: int | None = None
    ) -> ReadFileResult:
        args = {"path": path, "start_line": start_line, "end_line": end_line}
        return await self._call("read_file", args, ReadFileResult)

    async def find_symbol(self, symbol: str, max_results: int = 50) -> FindSymbolResult:
        args = {"symbol": symbol, "max_results": max_results}
        return await self._call("find_symbol", args, FindSymbolResult)

    async def git_log(self, path: str | None = None, max_count: int = 20) -> GitLogResult:
        return await self._call("git_log", {"path": path, "max_count": max_count}, GitLogResult)

    async def git_show(self, commit: str = "HEAD", path: str | None = None) -> GitTextResult:
        return await self._call("git_show", {"commit": commit, "path": path}, GitTextResult)

    async def git_diff(
        self, base: str = "HEAD", head: str | None = None, path: str | None = None
    ) -> GitTextResult:
        args = {"base": base, "head": head, "path": path}
        return await self._call("git_diff", args, GitTextResult)

    async def list_test_files(self, path: str = ".", max_results: int = 200) -> ListTestFilesResult:
        args = {"path": path, "max_results": max_results}
        return await self._call("list_test_files", args, ListTestFilesResult)

    async def check_patch(self, diff: str) -> CheckPatchResult:
        return await self._call("check_patch", {"diff": diff}, CheckPatchResult)


class PatchMCPClient(_MCPToolClient):
    """Client for the privileged patch server; used only after a human approval."""

    @classmethod
    @asynccontextmanager
    async def connect(
        cls,
        investigation_id: str,
        workspaces_dir: Path,
        database_path: Path,
        call_timeout: float = DEFAULT_CALL_TIMEOUT_SECONDS,
    ) -> AsyncIterator["PatchMCPClient"]:
        args = [
            "--investigation-id",
            investigation_id,
            "--workspaces-dir",
            str(workspaces_dir.resolve()),
            "--database-path",
            str(database_path.resolve()),
        ]
        async with _stdio_session("app.mcp_servers.patch_server", args) as session:
            yield cls(session, call_timeout)

    async def apply_patch(self) -> ApplyPatchResult:
        return await self._call("apply_patch", {}, ApplyPatchResult)

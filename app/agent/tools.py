"""Budgeted access to the repository MCP client, converting results to evidence."""

from typing import Protocol

from app.agent.models import Evidence, EvidenceType
from app.mcp_servers.models import (
    ApplyPatchResult,
    CheckPatchResult,
    FindSymbolResult,
    GitLogResult,
    ReadFileResult,
    SearchCodeResult,
    ToolResponse,
)

MAX_EXCERPT_CHARS = 2_000


class RepoToolClient(Protocol):
    """The subset of `RepoMCPClient` the agent uses (read-only)."""

    async def search_code(
        self, query: str, glob: str | None = None, max_results: int = 20, regex: bool = False
    ) -> SearchCodeResult: ...

    async def read_file(
        self, path: str, start_line: int = 1, end_line: int | None = None
    ) -> ReadFileResult: ...

    async def find_symbol(self, symbol: str, max_results: int = 50) -> FindSymbolResult: ...

    async def git_log(self, path: str | None = None, max_count: int = 20) -> GitLogResult: ...

    async def check_patch(self, diff: str) -> CheckPatchResult: ...


class PatchApplier(Protocol):
    """The privileged patch client. Only the post-approval graph node receives one."""

    async def apply_patch(self) -> ApplyPatchResult: ...


class ToolExecutor:
    """Enforces the tool-call budget; every MCP call made by the agent goes through here."""

    def __init__(self, client: RepoToolClient, max_calls: int) -> None:
        self._client = client
        self.max_calls = max_calls
        self.calls = 0

    @property
    def exhausted(self) -> bool:
        return self.calls >= self.max_calls

    def _take(self) -> bool:
        if self.exhausted:
            return False
        self.calls += 1
        return True

    async def search_code(
        self, query: str, glob: str | None, max_results: int
    ) -> SearchCodeResult | None:
        if not self._take():
            return None
        return await self._client.search_code(query, glob=glob, max_results=max_results)

    async def read_file(self, path: str, start_line: int, end_line: int) -> ReadFileResult | None:
        if not self._take():
            return None
        return await self._client.read_file(path, start_line=start_line, end_line=end_line)

    async def find_symbol(self, symbol: str) -> FindSymbolResult | None:
        if not self._take():
            return None
        return await self._client.find_symbol(symbol, max_results=20)

    async def git_log(self, path: str, max_count: int = 5) -> GitLogResult | None:
        if not self._take():
            return None
        return await self._client.git_log(path=path, max_count=max_count)

    async def check_patch(self, diff: str) -> CheckPatchResult:
        """Patch validation is not investigation; it does not consume the tool budget."""
        return await self._client.check_patch(diff)


def _clip(text: str) -> str:
    return text if len(text) <= MAX_EXCERPT_CHARS else text[:MAX_EXCERPT_CHARS] + "\n…"


def _require_id(result: ToolResponse) -> str:
    if not result.ok or not result.evidence_id:
        raise ValueError("only successful tool results can become evidence")
    return result.evidence_id


def evidence_from_search(
    result: SearchCodeResult, *, glob: str | None, kind: EvidenceType = EvidenceType.CODE
) -> Evidence:
    lines = [f"{m.path}:{m.line}: {m.snippet.strip()}" for m in result.matches]
    scope = f" in {glob}" if glob else ""
    return Evidence(
        evidence_id=_require_id(result),
        type=kind,
        source="search_code",
        excerpt=_clip("\n".join(lines)),
        summary=f"{len(result.matches)} match(es) for '{result.query}'{scope}"
        + (" (truncated)" if result.truncated else ""),
    )


def evidence_from_symbol(result: FindSymbolResult) -> Evidence:
    lines = [f"{m.path}:{m.line}: [{m.kind}] {m.snippet.strip()}" for m in result.matches]
    first = result.matches[0] if result.matches else None
    return Evidence(
        evidence_id=_require_id(result),
        type=EvidenceType.CODE,
        source="find_symbol",
        path=first.path if first and len(result.matches) == 1 else None,
        start_line=first.line if first and len(result.matches) == 1 else None,
        excerpt=_clip("\n".join(lines)),
        summary=f"{len(result.matches)} definition(s) of '{result.symbol}'",
    )


def evidence_from_read(result: ReadFileResult, reason: str) -> Evidence:
    text = "\n".join(f"{line.number:5} {line.text}" for line in result.lines)
    return Evidence(
        evidence_id=_require_id(result),
        type=EvidenceType.CODE,
        source="read_file",
        path=result.path,
        start_line=result.start_line,
        end_line=result.end_line,
        excerpt=_clip(text),
        summary=f"{result.path} lines {result.start_line}-{result.end_line}",
        relevance=reason,
    )


def evidence_from_log(result: GitLogResult) -> Evidence:
    lines = [f"{c.sha[:12]} {c.date} {c.author}: {c.subject}" for c in result.commits]
    return Evidence(
        evidence_id=_require_id(result),
        type=EvidenceType.GIT,
        source="git_log",
        path=result.path,
        excerpt=_clip("\n".join(lines)),
        summary=f"{len(result.commits)} recent commit(s) touching {result.path or 'repository'}",
    )

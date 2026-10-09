"""Read-only repository tools bound to one prepared investigation workspace.

All filesystem access goes through `resolve_inside`; all commands go through
the hardened `run_git`. Nothing here writes to the repository.
"""

import hashlib
import json
import re
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from app.agent.patches import (
    InvalidPatchError,
    ParsedPatch,
    check_hunk_reaches_eof,
    parse_unified_diff,
    patch_sha256,
)
from app.mcp_servers.models import (
    DEFAULT_SEARCH_RESULTS,
    MAX_LIST_ENTRIES,
    MAX_LOG_COUNT,
    MAX_READ_LINES,
    MAX_SEARCH_RESULTS,
    MAX_TEST_FILES,
    CheckPatchResult,
    CommitSummary,
    DirectoryEntry,
    FileLine,
    FindSymbolResult,
    GitLogResult,
    GitTextResult,
    ListDirectoryResult,
    ListTestFilesResult,
    ReadFileResult,
    SearchCodeResult,
    SearchMatch,
    SymbolMatch,
    ToolError,
    ToolErrorCode,
    ToolResponse,
)
from app.workspace.manager import WorkspaceManager, run_git
from app.workspace.paths import PathOutsideWorkspaceError, resolve_inside
from app.workspace.runner import CommandError, CommandResult, CommandTimeoutError

GIT_TIMEOUT_SECONDS = 30
MAX_FILE_BYTES = 2_000_000
MAX_LINE_CHARS = 1_000
MAX_SNIPPET_CHARS = 300
MAX_QUERY_CHARS = 200
MAX_GIT_TEXT_CHARS = 20_000

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
# Commit-ish: hex sha (abbreviated or full) or HEAD with optional ~N / ^N.
_REF_RE = re.compile(r"^(?:[0-9a-f]{4,40}|HEAD)(?:[~^][0-9]{0,3})*$")
_TEST_DIR_NAMES = {"test", "tests", "testing"}
# Shared by `git apply --check` and the privileged apply so both judge a patch alike.
# No --unidiff-zero: git keeps its default anchoring (hunks with no trailing context
# must match at end of file, hunks at line 1 at the start), and the validator
# requires standard context around every change.
GIT_APPLY_OPTIONS = ("--whitespace=nowarn",)


class ToolFailure(Exception):
    def __init__(self, code: ToolErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def _evidence_id(tool: str, args: dict[str, Any], payload: str) -> str:
    digest = hashlib.sha256(
        json.dumps({"tool": tool, "args": args, "payload": payload}, sort_keys=True).encode()
    ).hexdigest()
    return f"ev-{digest[:12]}"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _check_ref(ref: str) -> str:
    if not _REF_RE.match(ref):
        raise ToolFailure(ToolErrorCode.INVALID_ARGUMENT, f"invalid git ref: {ref!r}")
    return ref


def _check_glob(glob: str) -> str:
    if not glob or len(glob) > 200 or glob.startswith((":", "/", "-")) or ".." in glob:
        raise ToolFailure(ToolErrorCode.INVALID_ARGUMENT, f"invalid glob: {glob!r}")
    return glob


class RepoTools:
    def __init__(self, repo: Path) -> None:
        self.repo = repo.resolve()

    @classmethod
    def for_investigation(cls, manager: WorkspaceManager, investigation_id: str) -> "RepoTools":
        """Bind to an already-prepared workspace; fails if it is missing or not isolated."""
        repo = manager.repo_dir(investigation_id)
        if not (repo / ".git").is_dir():
            raise FileNotFoundError(f"workspace not prepared: {investigation_id}")
        manager.head(investigation_id)  # verifies git toplevel == workspace repo
        return cls(repo)

    # -- helpers -----------------------------------------------------------

    def _resolve(self, path: str) -> tuple[Path, str]:
        """Return (absolute path, normalized repo-relative posix path)."""
        try:
            resolved = resolve_inside(self.repo, path)
        except PathOutsideWorkspaceError as exc:
            raise ToolFailure(ToolErrorCode.PATH_REJECTED, str(exc)) from exc
        relative = resolved.relative_to(self.repo).as_posix()
        parts = PurePosixPath(relative).parts
        if ".git" in parts or any(part.startswith(".env") for part in parts):
            raise ToolFailure(ToolErrorCode.PATH_REJECTED, f"access to {path!r} is not allowed")
        return resolved, relative or "."

    def _git(self, *args: str, check: bool = True) -> CommandResult:
        try:
            return run_git(
                self.repo,
                *args,
                timeout=GIT_TIMEOUT_SECONDS,
                check=check,
                max_output_chars=MAX_GIT_TEXT_CHARS * 5,
            )
        except CommandTimeoutError as exc:
            raise ToolFailure(ToolErrorCode.TIMEOUT, str(exc)) from exc
        except CommandError as exc:
            raise ToolFailure(ToolErrorCode.COMMAND_FAILED, str(exc)) from exc

    def _run[R: ToolResponse](
        self, tool: str, args: dict[str, Any], result_type: type[R], body: Callable[[], R]
    ) -> R:
        try:
            result = body()
        except ToolFailure as exc:
            return result_type(ok=False, error=ToolError(code=exc.code, message=str(exc)))
        payload = result.model_dump_json(exclude={"evidence_id"})
        result.evidence_id = _evidence_id(tool, args, payload)
        return result

    # -- tools -------------------------------------------------------------

    def list_directory(self, path: str = ".") -> ListDirectoryResult:
        def body() -> ListDirectoryResult:
            directory, relative = self._resolve(path)
            if not directory.exists():
                raise ToolFailure(ToolErrorCode.NOT_FOUND, f"no such directory: {path!r}")
            if not directory.is_dir():
                raise ToolFailure(ToolErrorCode.NOT_A_DIRECTORY, f"not a directory: {path!r}")
            children = sorted(
                (
                    c
                    for c in directory.iterdir()
                    if c.name != ".git" and not c.name.startswith(".env")
                ),
                key=lambda c: c.name,
            )
            entries = []
            for child in children[:MAX_LIST_ENTRIES]:
                child_rel = child.relative_to(self.repo).as_posix()
                if child.is_symlink():
                    entries.append(DirectoryEntry(name=child.name, path=child_rel, kind="symlink"))
                elif child.is_dir():
                    entries.append(
                        DirectoryEntry(name=child.name, path=child_rel, kind="directory")
                    )
                elif child.is_file():
                    size = child.stat().st_size
                    entries.append(
                        DirectoryEntry(name=child.name, path=child_rel, kind="file", size=size)
                    )
                else:
                    entries.append(DirectoryEntry(name=child.name, path=child_rel, kind="other"))
            return ListDirectoryResult(
                path=relative, entries=entries, truncated=len(children) > MAX_LIST_ENTRIES
            )

        return self._run("list_directory", {"path": path}, ListDirectoryResult, body)

    def search_code(
        self,
        query: str,
        glob: str | None = None,
        max_results: int = DEFAULT_SEARCH_RESULTS,
        regex: bool = False,
    ) -> SearchCodeResult:
        args = {"query": query, "glob": glob, "max_results": max_results, "regex": regex}

        def body() -> SearchCodeResult:
            if not query or len(query) > MAX_QUERY_CHARS or "\n" in query:
                raise ToolFailure(ToolErrorCode.INVALID_ARGUMENT, "query must be 1-200 chars")
            if not 1 <= max_results <= MAX_SEARCH_RESULTS:
                raise ToolFailure(
                    ToolErrorCode.INVALID_ARGUMENT, f"max_results must be 1-{MAX_SEARCH_RESULTS}"
                )
            mode = "--extended-regexp" if regex else "--fixed-strings"
            cmd = ["grep", "--null", "-n", "-I", "--no-color", mode, "-e", query]
            if glob:
                cmd += ["--", f":(glob){_check_glob(glob)}"]
            matches, truncated = self._grep(cmd, max_results)
            return SearchCodeResult(query=query, matches=matches, truncated=truncated)

        return self._run("search_code", args, SearchCodeResult, body)

    def _grep(self, cmd: list[str], limit: int) -> tuple[list[SearchMatch], bool]:
        result = self._git(*cmd, check=False)
        if result.returncode == 1:  # no matches
            return [], False
        if result.returncode != 0:
            raise ToolFailure(ToolErrorCode.COMMAND_FAILED, result.stderr.strip()[:500])
        matches: list[SearchMatch] = []
        lines = result.stdout.splitlines()
        if result.stdout_truncated:
            lines = lines[:-2]  # drop the partial record and the truncation marker
        for raw in lines:
            parts = raw.split("\x00", 2)
            if len(parts) != 3 or not parts[1].isdigit():
                continue
            matches.append(
                SearchMatch(
                    path=parts[0], line=int(parts[1]), snippet=_clip(parts[2], MAX_SNIPPET_CHARS)
                )
            )
        truncated = result.stdout_truncated or len(matches) > limit
        return matches[:limit], truncated

    def read_file(
        self, path: str, start_line: int = 1, end_line: int | None = None
    ) -> ReadFileResult:
        args = {"path": path, "start_line": start_line, "end_line": end_line}

        def body() -> ReadFileResult:
            file_path, relative = self._resolve(path)
            if not file_path.exists():
                raise ToolFailure(ToolErrorCode.NOT_FOUND, f"no such file: {path!r}")
            if not file_path.is_file():
                raise ToolFailure(ToolErrorCode.NOT_A_FILE, f"not a regular file: {path!r}")
            if start_line < 1 or (end_line is not None and end_line < start_line):
                raise ToolFailure(ToolErrorCode.INVALID_ARGUMENT, "invalid line range")
            if file_path.stat().st_size > MAX_FILE_BYTES:
                raise ToolFailure(ToolErrorCode.TOO_LARGE, f"file exceeds {MAX_FILE_BYTES} bytes")
            data = file_path.read_bytes()
            if b"\x00" in data[:8192]:
                raise ToolFailure(ToolErrorCode.BINARY_FILE, f"binary file: {path!r}")

            all_lines = data.decode("utf-8", errors="replace").splitlines()
            requested_end = end_line if end_line is not None else start_line + MAX_READ_LINES - 1
            capped_end = min(requested_end, start_line + MAX_READ_LINES - 1, len(all_lines))
            lines = [
                FileLine(number=n, text=_clip(all_lines[n - 1], MAX_LINE_CHARS))
                for n in range(start_line, capped_end + 1)
            ]
            truncated = capped_end < min(requested_end, len(all_lines)) or (
                end_line is None and capped_end < len(all_lines)
            )
            return ReadFileResult(
                path=relative,
                start_line=start_line,
                end_line=capped_end,
                total_lines=len(all_lines),
                lines=lines,
                truncated=truncated,
            )

        return self._run("read_file", args, ReadFileResult, body)

    def find_symbol(self, symbol: str, max_results: int = 50) -> FindSymbolResult:
        args = {"symbol": symbol, "max_results": max_results}

        def body() -> FindSymbolResult:
            if not _IDENTIFIER_RE.match(symbol):
                raise ToolFailure(ToolErrorCode.INVALID_ARGUMENT, f"invalid symbol: {symbol!r}")
            if not 1 <= max_results <= MAX_SEARCH_RESULTS:
                raise ToolFailure(ToolErrorCode.INVALID_ARGUMENT, "invalid max_results")
            definition = (
                r"^[[:space:]]*(async[[:space:]]+)?(def|class)[[:space:]]+"
                rf"{symbol}([^A-Za-z0-9_]|$)"
            )
            assignment = rf"^{symbol}[[:space:]]*(:[^=]*)?="
            cmd = [
                "grep", "--null", "-n", "-I", "--no-color", "--extended-regexp",
                "-e", definition, "-e", assignment, "--", ":(glob)**/*.py",
            ]  # fmt: skip
            raw, truncated = self._grep(cmd, max_results)
            matches = []
            for match in raw:
                stripped = match.snippet.lstrip()
                if stripped.startswith("class "):
                    kind = "class"
                elif stripped.startswith(("def ", "async ")):
                    kind = "function"
                else:
                    kind = "assignment"
                matches.append(SymbolMatch(kind=kind, **match.model_dump()))
            return FindSymbolResult(symbol=symbol, matches=matches, truncated=truncated)

        return self._run("find_symbol", args, FindSymbolResult, body)

    def git_log(self, path: str | None = None, max_count: int = 20) -> GitLogResult:
        args = {"path": path, "max_count": max_count}

        def body() -> GitLogResult:
            if not 1 <= max_count <= MAX_LOG_COUNT:
                raise ToolFailure(
                    ToolErrorCode.INVALID_ARGUMENT, f"max_count must be 1-{MAX_LOG_COUNT}"
                )
            cmd = ["log", f"--max-count={max_count}", "--format=%H%x1f%an%x1f%aI%x1f%s%x1e"]
            relative = None
            if path is not None:
                _, relative = self._resolve(path)
                cmd += ["--", relative]
            out = self._git(*cmd).stdout
            commits = []
            for record in out.split("\x1e"):
                fields = record.strip().split("\x1f")
                if len(fields) == 4:
                    sha, author, date, subject = fields
                    commits.append(
                        CommitSummary(sha=sha, author=author, date=date, subject=subject)
                    )
            return GitLogResult(path=relative, commits=commits)

        return self._run("git_log", args, GitLogResult, body)

    def _git_text(self, cmd: list[str]) -> GitTextResult:
        result = self._git(*cmd)
        text = result.stdout
        truncated = result.stdout_truncated or len(text) > MAX_GIT_TEXT_CHARS
        return GitTextResult(text=text[:MAX_GIT_TEXT_CHARS], truncated=truncated)

    def git_show(self, commit: str = "HEAD", path: str | None = None) -> GitTextResult:
        args = {"commit": commit, "path": path}

        def body() -> GitTextResult:
            cmd = ["show", "--no-color", "--no-ext-diff", "--no-textconv", "--stat", "--patch"]
            cmd.append(_check_ref(commit))
            if path is not None:
                cmd += ["--", self._resolve(path)[1]]
            return self._git_text(cmd)

        return self._run("git_show", args, GitTextResult, body)

    def git_diff(
        self, base: str = "HEAD", head: str | None = None, path: str | None = None
    ) -> GitTextResult:
        """Diff `base` against `head`, or against the working tree if `head` is omitted."""
        args = {"base": base, "head": head, "path": path}

        def body() -> GitTextResult:
            cmd = ["diff", "--no-color", "--no-ext-diff", "--no-textconv", _check_ref(base)]
            if head is not None:
                cmd.append(_check_ref(head))
            if path is not None:
                cmd += ["--", self._resolve(path)[1]]
            return self._git_text(cmd)

        return self._run("git_diff", args, GitTextResult, body)

    def list_test_files(self, path: str = ".", max_results: int = 200) -> ListTestFilesResult:
        args = {"path": path, "max_results": max_results}

        def body() -> ListTestFilesResult:
            if not 1 <= max_results <= MAX_TEST_FILES:
                raise ToolFailure(ToolErrorCode.INVALID_ARGUMENT, "invalid max_results")
            _, relative = self._resolve(path)
            out = self._git("ls-files", "-z", "--", relative).stdout
            files = sorted(f for f in out.split("\x00") if f and _looks_like_test(f))
            return ListTestFilesResult(
                path=relative, files=files[:max_results], truncated=len(files) > max_results
            )

        return self._run("list_test_files", args, ListTestFilesResult, body)

    # -- patch validation (no writes) ----------------------------------------

    def validate_patch(self, diff: str) -> ParsedPatch:
        """Structural + path checks, then `git apply --check` against the working tree."""
        try:
            parsed = parse_unified_diff(diff)
        except InvalidPatchError as exc:
            raise ToolFailure(ToolErrorCode.PATCH_REJECTED, str(exc)) from exc
        line_counts: dict[str, int] = {}
        for path in parsed.files:
            resolved, _ = self._resolve(path)  # also rejects symlink escapes
            if not resolved.is_file() or resolved.is_symlink():
                raise ToolFailure(ToolErrorCode.NOT_FOUND, f"patch targets missing file {path!r}")
            if resolved.stat().st_size > MAX_FILE_BYTES:
                raise ToolFailure(ToolErrorCode.TOO_LARGE, f"patch target too large: {path!r}")
            line_counts[path] = len(resolved.read_bytes().splitlines())
        try:
            for hunk in parsed.hunks:
                check_hunk_reaches_eof(hunk, line_counts[hunk.path])
        except InvalidPatchError as exc:
            raise ToolFailure(ToolErrorCode.PATCH_REJECTED, str(exc)) from exc
        # The patch file lives outside the repository; --check reads but never writes.
        with tempfile.TemporaryDirectory(prefix="bugtrace-check-") as tmp:
            patch_file = Path(tmp) / "check.diff"
            patch_file.write_text(parsed.diff, encoding="utf-8")
            result = self._git("apply", "--check", *GIT_APPLY_OPTIONS, str(patch_file), check=False)
        if result.returncode != 0:
            detail = " ".join(result.stderr.strip().splitlines()[-3:]) or "no details"
            raise ToolFailure(ToolErrorCode.PATCH_REJECTED, f"git apply --check failed: {detail}")
        return parsed

    def check_patch(self, diff: str) -> CheckPatchResult:
        def body() -> CheckPatchResult:
            return CheckPatchResult(files=self.validate_patch(diff).files)

        return self._run("check_patch", {"sha256": patch_sha256(diff)}, CheckPatchResult, body)


def _looks_like_test(path: str) -> bool:
    pure = PurePosixPath(path)
    if pure.suffix != ".py":
        return False
    name = pure.name
    if (
        name.startswith("test_")
        or name.endswith(("_test.py", "_tests.py"))
        or name == "conftest.py"
    ):
        return True
    return any(part in _TEST_DIR_NAMES for part in pure.parts[:-1]) and name != "__init__.py"

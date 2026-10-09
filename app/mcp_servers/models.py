"""Typed inputs/outputs for the read-only repository MCP tools."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

MAX_READ_LINES = 300
MAX_SEARCH_RESULTS = 100
DEFAULT_SEARCH_RESULTS = 20
MAX_LOG_COUNT = 100
MAX_LIST_ENTRIES = 500
MAX_TEST_FILES = 500


class ToolErrorCode(StrEnum):
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    PATH_REJECTED = "PATH_REJECTED"
    NOT_FOUND = "NOT_FOUND"
    NOT_A_FILE = "NOT_A_FILE"
    NOT_A_DIRECTORY = "NOT_A_DIRECTORY"
    BINARY_FILE = "BINARY_FILE"
    TOO_LARGE = "TOO_LARGE"
    TIMEOUT = "TIMEOUT"
    COMMAND_FAILED = "COMMAND_FAILED"
    PATCH_REJECTED = "PATCH_REJECTED"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"


class ToolError(BaseModel):
    code: ToolErrorCode
    message: str


class ToolResponse(BaseModel):
    """Common envelope: `ok` + `error` on failure, `evidence_id` on success."""

    ok: bool = True
    error: ToolError | None = None
    evidence_id: str | None = None
    truncated: bool = False


class DirectoryEntry(BaseModel):
    name: str
    path: str
    kind: Literal["file", "directory", "symlink", "other"]
    size: int | None = None


class ListDirectoryResult(ToolResponse):
    path: str = ""
    entries: list[DirectoryEntry] = Field(default_factory=list)


class SearchMatch(BaseModel):
    path: str
    line: int
    snippet: str


class SearchCodeResult(ToolResponse):
    query: str = ""
    matches: list[SearchMatch] = Field(default_factory=list)


class FileLine(BaseModel):
    number: int
    text: str


class ReadFileResult(ToolResponse):
    path: str = ""
    start_line: int = 0
    end_line: int = 0
    total_lines: int = 0
    lines: list[FileLine] = Field(default_factory=list)


class SymbolMatch(BaseModel):
    path: str
    line: int
    kind: Literal["function", "class", "assignment"]
    snippet: str


class FindSymbolResult(ToolResponse):
    symbol: str = ""
    matches: list[SymbolMatch] = Field(default_factory=list)


class CommitSummary(BaseModel):
    sha: str
    author: str
    date: str
    subject: str


class GitLogResult(ToolResponse):
    path: str | None = None
    commits: list[CommitSummary] = Field(default_factory=list)


class GitTextResult(ToolResponse):
    """Output of `git show` / `git diff`, bounded."""

    text: str = ""


class ListTestFilesResult(ToolResponse):
    path: str = ""
    files: list[str] = Field(default_factory=list)


class CheckPatchResult(ToolResponse):
    """`git apply --check` outcome; `ok` means the patch applies cleanly. Never mutates."""

    files: list[str] = Field(default_factory=list)


class ApplyPatchResult(ToolResponse):
    applied: bool = False
    files: list[str] = Field(default_factory=list)
    stat: str = ""
    diff: str = ""

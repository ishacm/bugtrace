"""Strict unified-diff validation shared by patch proposal and privileged application.

Accepted: in-place content edits to existing text files, `--- a/<path>` /
`+++ b/<path>` headers (optionally preceded by `diff --git` and `index` lines)
and hunks whose line counts match their headers. Rejected: new, deleted,
renamed, mode-changed or binary files, paths that are absolute, contain `..`,
touch `.git` or `.env*`, and anything that is not a well-formed hunk.
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

MAX_PATCH_CHARS = 60_000
MAX_PATCH_FILES = 10

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")
_PATH_RE = re.compile(r"^[A-Za-z0-9_./@+-]+$")
_FORBIDDEN_HEADERS = (
    "new file mode",
    "deleted file mode",
    "old mode",
    "new mode",
    "rename from",
    "rename to",
    "copy from",
    "copy to",
    "similarity index",
    "dissimilarity index",
    "Binary files",
    "GIT binary patch",
)


class InvalidPatchError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedPatch:
    diff: str  # normalized text that was validated; apply exactly this
    files: list[str]
    added: int
    removed: int


def patch_sha256(diff: str) -> str:
    return hashlib.sha256(diff.encode()).hexdigest()


def check_patch_path(path: str) -> str:
    if not path or len(path) > 300 or not _PATH_RE.match(path):
        raise InvalidPatchError(f"invalid path in patch: {path!r}")
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or "." in pure.parts or "//" in path:
        raise InvalidPatchError(f"path escapes the repository: {path!r}")
    if any(part == ".git" or part.startswith(".env") for part in pure.parts):
        raise InvalidPatchError(f"patch may not touch {path!r}")
    return pure.as_posix()


def _header_path(line: str, prefix: str, side: str) -> str:
    raw = line[len(prefix) :].split("\t", 1)[0].strip()
    if raw == "/dev/null":
        raise InvalidPatchError("patches may only modify existing files (no create/delete)")
    if not raw.startswith(f"{side}/"):
        raise InvalidPatchError(f"expected '{prefix}{side}/<path>', got {line!r}")
    return check_patch_path(raw[2:])


def _is_body_line(line: str) -> bool:
    return line[:1] in (" ", "+", "-") and not line.startswith("--- ")


def parse_unified_diff(diff: str) -> ParsedPatch:
    """Validate `diff` and return its normalized form; raise InvalidPatchError otherwise."""
    if not diff.strip():
        raise InvalidPatchError("patch is empty")
    if len(diff) > MAX_PATCH_CHARS:
        raise InvalidPatchError(f"patch exceeds {MAX_PATCH_CHARS} characters")
    if "\x00" in diff or "\r" in diff:
        raise InvalidPatchError("patch contains NUL or carriage-return characters")

    lines = diff.strip("\n").split("\n")
    out: list[str] = []
    files: list[str] = []
    added = removed = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(_FORBIDDEN_HEADERS):
            raise InvalidPatchError(f"unsupported patch header: {line!r}")
        if line.startswith(("diff --git ", "index ")):
            out.append(line)
            i += 1
            continue
        if not line.startswith("--- "):
            raise InvalidPatchError(f"unexpected line outside a file section: {line!r}")
        if i + 1 >= len(lines) or not lines[i + 1].startswith("+++ "):
            raise InvalidPatchError("'---' header must be followed by '+++'")
        old = _header_path(line, "--- ", "a")
        new = _header_path(lines[i + 1], "+++ ", "b")
        if old != new:
            raise InvalidPatchError(f"renames are not allowed: {old!r} -> {new!r}")
        if new in files:
            raise InvalidPatchError(f"file appears twice in patch: {new!r}")
        files.append(new)
        out += [f"--- a/{new}", f"+++ b/{new}"]
        i += 2

        hunks = 0
        while i < len(lines) and lines[i].startswith("@@"):
            match = _HUNK_RE.match(lines[i])
            if not match:
                raise InvalidPatchError(f"malformed hunk header: {lines[i]!r}")
            old_left = int(match.group(2) or 1)
            new_left = int(match.group(4) or 1)
            out.append(lines[i])
            i += 1
            changes = 0
            while old_left > 0 or new_left > 0:
                if i >= len(lines):
                    raise InvalidPatchError("hunk is shorter than its header declares")
                body = lines[i] or " "  # models often drop the space on blank context lines
                tag = body[0]
                if tag == " ":
                    old_left -= 1
                    new_left -= 1
                elif tag == "-":
                    old_left -= 1
                    removed += 1
                    changes += 1
                elif tag == "+":
                    new_left -= 1
                    added += 1
                    changes += 1
                elif tag != "\\":
                    raise InvalidPatchError(f"invalid hunk line: {body!r}")
                if old_left < 0 or new_left < 0:
                    raise InvalidPatchError("hunk is longer than its header declares")
                out.append(body)
                i += 1
            while i < len(lines) and lines[i].startswith("\\"):
                out.append(lines[i])
                i += 1
            if i < len(lines) and _is_body_line(lines[i]):
                raise InvalidPatchError("hunk is longer than its header declares")
            if changes == 0:
                raise InvalidPatchError("hunk contains no changes")
            hunks += 1
        if hunks == 0:
            raise InvalidPatchError(f"no hunks for {new!r}")

    if not files:
        raise InvalidPatchError("patch has no file sections")
    if len(files) > MAX_PATCH_FILES:
        raise InvalidPatchError(f"patch touches more than {MAX_PATCH_FILES} files")
    return ParsedPatch(diff="\n".join(out) + "\n", files=files, added=added, removed=removed)

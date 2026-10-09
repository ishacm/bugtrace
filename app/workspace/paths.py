"""Path safety helpers for per-investigation workspaces."""

from pathlib import Path, PurePosixPath


class PathOutsideWorkspaceError(ValueError):
    pass


def resolve_inside(root: Path, relative: str) -> Path:
    """Resolve a repository-relative path, guaranteeing it stays inside `root`.

    Rejects absolute paths, `..` components, NUL bytes, and symlinks that
    resolve outside the root.
    """
    if not relative or "\x00" in relative:
        raise PathOutsideWorkspaceError("path must be a non-empty string without NUL bytes")
    candidate = PurePosixPath(relative.replace("\\", "/"))
    if candidate.is_absolute() or (candidate.parts and candidate.parts[0].endswith(":")):
        raise PathOutsideWorkspaceError(f"absolute paths are not allowed: {relative!r}")
    if ".." in candidate.parts:
        raise PathOutsideWorkspaceError(f"path traversal is not allowed: {relative!r}")

    resolved_root = root.resolve()
    resolved = (resolved_root / candidate).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise PathOutsideWorkspaceError(f"path escapes workspace: {relative!r}")
    return resolved

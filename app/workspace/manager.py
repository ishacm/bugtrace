"""Per-investigation repository workspaces pinned to a SWE-bench base commit."""

import re
import shutil
import time
import uuid
from pathlib import Path

from pydantic import BaseModel

from app.data.models import PublicCase
from app.workspace.runner import MAX_OUTPUT_CHARS, CommandError, CommandResult, run_command

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_ALLOWED_URL_PREFIXES = ("https://", "file://")

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class WorkspaceError(RuntimeError):
    pass


class Workspace(BaseModel):
    investigation_id: str
    instance_id: str
    repo_path: Path
    base_commit: str


def run_git(
    repo: Path,
    *args: str,
    timeout: float,
    check: bool = True,
    max_output_chars: int = MAX_OUTPUT_CHARS,
) -> CommandResult:
    """Run git confined to `repo`; the single hardened git entry point."""
    # Ceiling dirs stop git from walking up into an enclosing repository
    # (e.g. the BugTrace checkout when workspaces live under var/).
    env = {
        "GIT_CEILING_DIRECTORIES": str(repo.parent),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "https:file",
    }
    return run_command(
        ["git", "-c", "core.fsmonitor=false", *args],
        cwd=repo,
        timeout=timeout,
        env=env,
        check=check,
        max_output_chars=max_output_chars,
    )


def new_investigation_id() -> str:
    return str(uuid.uuid4())


def _validate_investigation_id(investigation_id: str) -> str:
    try:
        parsed = uuid.UUID(investigation_id)
    except (ValueError, TypeError) as exc:
        raise WorkspaceError(f"invalid investigation id: {investigation_id!r}") from exc
    if str(parsed) != investigation_id:
        raise WorkspaceError(f"investigation id must be canonical UUID: {investigation_id!r}")
    return investigation_id


def github_url(repo: str) -> str:
    if not _REPO_RE.match(repo) or ".." in repo:
        raise WorkspaceError(f"invalid repository name: {repo!r}")
    return f"https://github.com/{repo}.git"


class WorkspaceManager:
    def __init__(
        self,
        root: Path,
        *,
        git_timeout: float = 600,
        source_root: Path = PROJECT_ROOT,
    ) -> None:
        self.root = root.resolve()
        self.git_timeout = git_timeout
        self.source_root = source_root.resolve()
        if self.root == self.source_root or self.source_root.is_relative_to(self.root):
            raise WorkspaceError("workspace root must not contain the BugTrace source tree")

    def workspace_dir(self, investigation_id: str) -> Path:
        return self.root / _validate_investigation_id(investigation_id)

    def repo_dir(self, investigation_id: str) -> Path:
        return self.workspace_dir(investigation_id) / "repo"

    def _git(self, repo: Path, *args: str, timeout: float | None = None) -> str:
        return run_git(repo, *args, timeout=timeout or self.git_timeout).stdout.strip()

    def _assert_isolated(self, repo: Path) -> None:
        if repo == self.source_root or repo.is_relative_to(self.source_root / ".git"):
            raise WorkspaceError("refusing to operate on the BugTrace source repository")
        toplevel = Path(self._git(repo, "rev-parse", "--show-toplevel", timeout=30)).resolve()
        if toplevel != repo.resolve():
            raise WorkspaceError(f"git toplevel {toplevel} is not the workspace repo {repo}")

    def head(self, investigation_id: str) -> str:
        repo = self.repo_dir(investigation_id)
        self._assert_isolated(repo)
        return self._git(repo, "rev-parse", "HEAD", timeout=30)

    def prepare(
        self, investigation_id: str, case: PublicCase, remote_url: str | None = None
    ) -> Workspace:
        """Clone `case.repo` into the workspace and check out `case.base_commit`."""
        if not _SHA_RE.match(case.base_commit):
            raise WorkspaceError(f"base_commit must be a 40-char sha: {case.base_commit!r}")
        url = remote_url or github_url(case.repo)
        if not url.startswith(_ALLOWED_URL_PREFIXES):
            raise WorkspaceError(f"unsupported remote url: {url!r}")

        repo = self.repo_dir(investigation_id)
        workspace = Workspace(
            investigation_id=investigation_id,
            instance_id=case.instance_id,
            repo_path=repo,
            base_commit=case.base_commit,
        )
        if (repo / ".git").is_dir():
            try:
                if self.head(investigation_id) == case.base_commit:
                    return workspace
            except (CommandError, WorkspaceError):
                pass
            self.cleanup(investigation_id)

        repo.mkdir(parents=True, exist_ok=True)
        try:
            self._git(repo, "init", "--quiet", timeout=30)
            self._assert_isolated(repo)
            self._git(repo, "remote", "add", "origin", url, timeout=30)
            self._fetch_commit(repo, case.base_commit)
            self._git(repo, "checkout", "--quiet", "--detach", case.base_commit)
            head = self._git(repo, "rev-parse", "HEAD", timeout=30)
        except CommandError as exc:
            self.cleanup(investigation_id)
            raise WorkspaceError(f"failed to prepare {case.instance_id}: {exc}") from exc

        if head != case.base_commit:
            self.cleanup(investigation_id)
            raise WorkspaceError(f"HEAD {head} does not match base_commit {case.base_commit}")
        return workspace

    def _fetch_commit(self, repo: Path, sha: str) -> None:
        try:
            # Fast path: shallow fetch of exactly the base commit.
            self._git(repo, "fetch", "--quiet", "--depth", "1", "origin", sha)
        except CommandError:
            # Fallback for servers that refuse fetching an arbitrary sha:
            # blobless full-history fetch, then check out the sha locally.
            self._git(repo, "fetch", "--quiet", "--filter=blob:none", "--tags", "origin")

    def cleanup(self, investigation_id: str) -> None:
        path = self.workspace_dir(investigation_id)
        if path.exists():
            shutil.rmtree(path)

    def cleanup_stale(self, max_age_seconds: float) -> list[str]:
        """Remove workspaces whose directory is older than `max_age_seconds`."""
        if not self.root.is_dir():
            return []
        cutoff = time.time() - max_age_seconds
        removed = []
        for child in self.root.iterdir():
            try:
                _validate_investigation_id(child.name)
            except WorkspaceError:
                continue
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child)
                removed.append(child.name)
        return removed

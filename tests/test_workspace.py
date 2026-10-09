import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from app.data.models import PublicCase
from app.workspace.manager import (
    PROJECT_ROOT,
    WorkspaceError,
    WorkspaceManager,
    github_url,
    new_investigation_id,
)
from app.workspace.paths import PathOutsideWorkspaceError, resolve_inside
from app.workspace.runner import CommandError, CommandTimeoutError, run_command


def _git(cwd: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    return run_command(["git", *args], cwd=cwd, timeout=30, env=env).stdout.strip()


@pytest.fixture(scope="module")
def fixture_repo(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, list[str]]:
    """Tiny origin repo with three commits; returns (path, [sha1, sha2, sha3])."""
    origin = tmp_path_factory.mktemp("origin")
    _git(origin, "init", "--quiet", "--initial-branch=main")
    shas = []
    for i in range(3):
        (origin / "module.py").write_text(f"VERSION = {i}\n")
        _git(origin, "add", "module.py")
        _git(origin, "commit", "--quiet", "-m", f"commit {i}")
        shas.append(_git(origin, "rev-parse", "HEAD"))
    return origin, shas


def _case(sha: str) -> PublicCase:
    return PublicCase(
        instance_id="owner__fixture-1",
        repo="owner/fixture",
        problem_statement="fixture",
        base_commit=sha,
    )


@pytest.fixture
def manager(tmp_path: Path) -> WorkspaceManager:
    return WorkspaceManager(tmp_path / "workspaces", git_timeout=60)


def test_prepare_checks_out_exact_base_commit(
    manager: WorkspaceManager, fixture_repo: tuple[Path, list[str]]
) -> None:
    origin, shas = fixture_repo
    inv = new_investigation_id()
    ws = manager.prepare(inv, _case(shas[1]), remote_url=origin.as_uri())

    assert ws.repo_path == manager.root / inv / "repo"
    assert manager.head(inv) == shas[1]
    assert (ws.repo_path / "module.py").read_text() == "VERSION = 1\n"


def test_prepare_is_idempotent_for_existing_workspace(
    manager: WorkspaceManager, fixture_repo: tuple[Path, list[str]]
) -> None:
    origin, shas = fixture_repo
    inv = new_investigation_id()
    manager.prepare(inv, _case(shas[0]), remote_url=origin.as_uri())
    manager.prepare(inv, _case(shas[0]), remote_url=origin.as_uri())
    assert manager.head(inv) == shas[0]


def test_workspaces_are_isolated_per_investigation(
    manager: WorkspaceManager, fixture_repo: tuple[Path, list[str]]
) -> None:
    origin, shas = fixture_repo
    a, b = new_investigation_id(), new_investigation_id()
    ws_a = manager.prepare(a, _case(shas[0]), remote_url=origin.as_uri())
    ws_b = manager.prepare(b, _case(shas[2]), remote_url=origin.as_uri())

    (ws_a.repo_path / "module.py").write_text("MUTATED\n")
    assert (ws_b.repo_path / "module.py").read_text() == "VERSION = 2\n"
    assert manager.head(a) == shas[0]
    assert manager.head(b) == shas[2]

    manager.cleanup(a)
    assert not manager.workspace_dir(a).exists()
    assert ws_b.repo_path.is_dir()


def test_unknown_commit_fails_cleanly(
    manager: WorkspaceManager, fixture_repo: tuple[Path, list[str]]
) -> None:
    origin, _ = fixture_repo
    inv = new_investigation_id()
    with pytest.raises(WorkspaceError, match="failed to prepare"):
        manager.prepare(inv, _case("f" * 40), remote_url=origin.as_uri())
    assert not manager.workspace_dir(inv).exists()


def test_unreachable_remote_fails_cleanly(manager: WorkspaceManager, tmp_path: Path) -> None:
    inv = new_investigation_id()
    missing = (tmp_path / "does-not-exist").as_uri()
    with pytest.raises(WorkspaceError):
        manager.prepare(inv, _case("a" * 40), remote_url=missing)
    assert not manager.workspace_dir(inv).exists()


@pytest.mark.parametrize(
    "url", ["ssh://evil/repo", "ext::sh -c touch% /tmp/pwned", "/abs/path", "git@github.com:x/y"]
)
def test_rejects_unsupported_remote_urls(manager: WorkspaceManager, url: str) -> None:
    with pytest.raises(WorkspaceError, match="unsupported remote"):
        manager.prepare(new_investigation_id(), _case("a" * 40), remote_url=url)


@pytest.mark.parametrize("sha", ["HEAD", "main", "abc123", "--upload-pack=x", "G" * 40])
def test_rejects_non_sha_base_commit(manager: WorkspaceManager, sha: str) -> None:
    with pytest.raises(WorkspaceError, match="40-char sha"):
        manager.prepare(new_investigation_id(), _case(sha))


@pytest.mark.parametrize("repo", ["../etc", "owner/repo/../../x", "owner", "a b/c", "o/r;x"])
def test_rejects_invalid_repo_names(repo: str) -> None:
    with pytest.raises(WorkspaceError):
        github_url(repo)


@pytest.mark.parametrize("inv", ["../escape", "..", "", "not-a-uuid", "/abs/x"])
def test_rejects_invalid_investigation_ids(manager: WorkspaceManager, inv: str) -> None:
    with pytest.raises(WorkspaceError):
        manager.workspace_dir(inv)


def test_root_must_not_contain_source_tree() -> None:
    with pytest.raises(WorkspaceError):
        WorkspaceManager(PROJECT_ROOT)
    with pytest.raises(WorkspaceError):
        WorkspaceManager(PROJECT_ROOT.parent)


def test_git_never_walks_up_into_enclosing_repo(
    manager: WorkspaceManager, fixture_repo: tuple[Path, list[str]]
) -> None:
    # Nest the workspace root inside another git repo, then break the workspace's .git.
    origin, shas = fixture_repo
    outer = manager.root.parent
    _git(outer, "init", "--quiet")
    inv = new_investigation_id()
    ws = manager.prepare(inv, _case(shas[0]), remote_url=origin.as_uri())

    shutil.rmtree(ws.repo_path / ".git")
    with pytest.raises((WorkspaceError, CommandError)):
        manager.head(inv)


def test_cleanup_stale_only_removes_old_uuid_dirs(manager: WorkspaceManager) -> None:
    old, fresh = new_investigation_id(), new_investigation_id()
    for inv in (old, fresh):
        manager.workspace_dir(inv).mkdir(parents=True)
    (manager.root / "not-a-workspace").mkdir()
    past = time.time() - 3600
    os.utime(manager.workspace_dir(old), (past, past))

    assert manager.cleanup_stale(max_age_seconds=60) == [old]
    assert manager.workspace_dir(fresh).exists()
    assert (manager.root / "not-a-workspace").exists()


# --- path safety -----------------------------------------------------------


def test_resolve_inside_accepts_nested_paths(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    assert resolve_inside(tmp_path, "pkg/mod.py") == tmp_path.resolve() / "pkg" / "mod.py"
    assert resolve_inside(tmp_path, "./pkg") == tmp_path.resolve() / "pkg"


@pytest.mark.parametrize(
    "bad",
    [
        "../secret",
        "pkg/../../secret",
        "/etc/passwd",
        "..\\..\\secret",
        "C:\\Windows",
        "a\x00b",
        "",
    ],
)
def test_resolve_inside_rejects_traversal(tmp_path: Path, bad: str) -> None:
    with pytest.raises(PathOutsideWorkspaceError):
        resolve_inside(tmp_path, bad)


def test_resolve_inside_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("secret")
    (root / "link").symlink_to(tmp_path / "outside.txt")
    with pytest.raises(PathOutsideWorkspaceError):
        resolve_inside(root, "link")


# --- command runner --------------------------------------------------------


def test_run_command_times_out(tmp_path: Path) -> None:
    with pytest.raises(CommandTimeoutError):
        run_command([sys.executable, "-c", "import time; time.sleep(5)"], cwd=tmp_path, timeout=0.2)


def test_run_command_reports_nonzero_exit(tmp_path: Path) -> None:
    with pytest.raises(CommandError) as info:
        run_command([sys.executable, "-c", "import sys; sys.exit(3)"], cwd=tmp_path, timeout=10)
    assert info.value.result is not None
    assert info.value.result.returncode == 3


def test_run_command_missing_executable(tmp_path: Path) -> None:
    with pytest.raises(CommandError):
        run_command(["definitely-not-a-binary-xyz"], cwd=tmp_path, timeout=5)


@pytest.mark.parametrize("args", ["git status", [], ["git", 1]])
def test_run_command_rejects_non_list_args(tmp_path: Path, args: object) -> None:
    with pytest.raises(ValueError):
        run_command(args, cwd=tmp_path, timeout=5)  # type: ignore[arg-type]


def test_run_command_requires_absolute_existing_cwd(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        run_command(["git", "--version"], cwd=Path("relative"), timeout=5)
    with pytest.raises(ValueError):
        run_command(["git", "--version"], cwd=tmp_path / "missing", timeout=5)


def test_run_command_scrubs_git_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_DIR", str(PROJECT_ROOT / ".git"))
    result = run_command(
        [sys.executable, "-c", "import os; print(os.environ.get('GIT_DIR', ''))"],
        cwd=tmp_path,
        timeout=10,
    )
    assert result.stdout.strip() == ""


def test_no_shell_true_in_app_or_scripts() -> None:
    for path in [*(PROJECT_ROOT / "app").rglob("*.py"), *(PROJECT_ROOT / "scripts").glob("*.py")]:
        assert "shell=True" not in path.read_text(), path

from pathlib import Path

import pytest

from app.workspace.runner import run_command

_COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}

CORE_V1 = '''"""Core widgets."""

DEFAULT_LIMIT = 10
limit_hint: int = 3


class Widget:
    def __init__(self, size):
        self.size = size


def compute_total(items):
    return sum(items) + 1  # BUG: off by one


async def fetch_data():
    return compute_total([1, 2])
'''


@pytest.fixture(autouse=True)
def _no_model_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never see a real model key (CI has none; a developer shell might)."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


CORE_V2 = CORE_V1.replace("sum(items) + 1  # BUG: off by one", "sum(items)")


def fixture_git(cwd: Path, *args: str) -> str:
    """Run git for building fixture repos (not the hardened workspace path)."""
    return run_command(["git", *args], cwd=cwd, timeout=30, env=_COMMIT_ENV).stdout.strip()


@pytest.fixture(scope="session")
def fixture_repo(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, list[str]]:
    """Tiny origin repo with three commits; returns (path, [sha1, sha2, sha3]).

    `module.py` holds VERSION = <i> at commit i. Commit 2 also fixes a bug in
    `pkg/core.py`. Includes tests, a large file, a binary file and a tracked .env.
    """
    origin = tmp_path_factory.mktemp("origin")
    fixture_git(origin, "init", "--quiet", "--initial-branch=main")
    files = {
        "pkg/__init__.py": "",
        "pkg/core.py": CORE_V1,
        "pkg/usage.py": "from pkg.core import compute_total\n\nx = compute_total([3])\n",
        "pkg/big.py": "".join(f"LINE_{n} = {n}\n" for n in range(1, 451)),
        "tests/__init__.py": "",
        "tests/test_core.py": "def test_total():\n    assert True\n",
        "tests/helpers.py": "HELPER = 1\n",
        "pkg/core_test.py": "def test_x():\n    pass\n",
        "conftest.py": "",
        "docs/readme.md": "compute_total docs\n",
        ".env": "SECRET_TOKEN=do-not-read\n",
    }
    for rel, content in files.items():
        (origin / rel).parent.mkdir(parents=True, exist_ok=True)
        (origin / rel).write_text(content)
    (origin / "pkg/blob.bin").write_bytes(b"\x00\x01\x02binary")

    shas = []
    for i in range(3):
        (origin / "module.py").write_text(f"VERSION = {i}\n")
        if i == 2:
            (origin / "pkg/core.py").write_text(CORE_V2)
        fixture_git(origin, "add", "-A")
        fixture_git(origin, "commit", "--quiet", "-m", f"commit {i}")
        shas.append(fixture_git(origin, "rev-parse", "HEAD"))
    return origin, shas

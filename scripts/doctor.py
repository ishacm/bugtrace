"""Local environment checks for BugTrace. Prints PASS/WARNING/FAIL per check."""

import shutil
import subprocess  # noqa: S404
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TIMEOUT_SECONDS = 10

PASS, WARNING, FAIL = "PASS", "WARNING", "FAIL"


def _run_version(executable: str) -> str | None:
    path = shutil.which(executable)
    if path is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603
            [path, "--version"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or result.stderr.strip()


def check_python() -> tuple[str, str]:
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info[:2] == (3, 12):
        return PASS, f"Python {version}"
    return FAIL, f"Python {version} (expected 3.12)"


def check_git() -> tuple[str, str]:
    version = _run_version("git")
    if version is None:
        return FAIL, "git not found on PATH"
    return PASS, version


def check_writable_dirs() -> tuple[str, str]:
    data_dir = ROOT / "var"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=data_dir):
            pass
    except OSError as exc:
        return FAIL, f"{data_dir} not writable: {exc}"
    return PASS, f"{data_dir.relative_to(ROOT)}/ is writable"


def check_env_file() -> tuple[str, str]:
    if (ROOT / ".env").is_file():
        return PASS, ".env present"
    return WARNING, ".env missing (copy .env.example to .env)"


def check_docker() -> tuple[str, str]:
    version = _run_version("docker")
    if version is None:
        return WARNING, "docker not available (required only for V2 evaluation)"
    return PASS, version


CHECKS = [
    ("Python", check_python),
    ("Git", check_git),
    ("Writable dirs", check_writable_dirs),
    (".env", check_env_file),
    ("Docker", check_docker),
]


def main() -> int:
    failures = 0
    for name, check in CHECKS:
        status, detail = check()
        failures += status == FAIL
        print(f"[{status:<7}] {name:<14} {detail}")
    print()
    if failures:
        print(f"doctor: {failures} check(s) failed")
        return 1
    print("doctor: all required checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""The single subprocess entry point for workspace commands.

Arguments are always an explicit list, cwd is always explicit, a timeout is
always applied, and output is bounded. There is no shell.
"""

import os
import subprocess  # noqa: S404
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel

MAX_OUTPUT_CHARS = 20_000

# Git env vars that could redirect commands to another repository.
_SCRUBBED_ENV_PREFIXES = ("GIT_",)


class CommandResult(BaseModel):
    args: list[str]
    cwd: Path
    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False


class CommandError(RuntimeError):
    def __init__(self, message: str, result: CommandResult | None = None) -> None:
        super().__init__(message)
        self.result = result


class CommandTimeoutError(CommandError):
    pass


def _bounded(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def _safe_env(extra: dict[str, str] | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(_SCRUBBED_ENV_PREFIXES)}
    env.update(extra or {})
    return env


def run_command(
    args: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    env: dict[str, str] | None = None,
    check: bool = True,
    max_output_chars: int = MAX_OUTPUT_CHARS,
) -> CommandResult:
    if isinstance(args, str) or not args or not all(isinstance(a, str) for a in args):
        raise ValueError("args must be a non-empty sequence of strings")
    if not cwd.is_absolute() or not cwd.is_dir():
        raise ValueError(f"cwd must be an existing absolute directory: {cwd}")
    if timeout <= 0:
        raise ValueError("timeout must be positive")

    arg_list = list(args)
    try:
        completed = subprocess.run(  # noqa: S603
            arg_list,
            cwd=cwd,
            env=_safe_env(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandTimeoutError(f"{arg_list[0]} timed out after {timeout}s") from exc
    except OSError as exc:
        raise CommandError(f"failed to run {arg_list[0]}: {exc}") from exc

    result = CommandResult(
        args=arg_list,
        cwd=cwd,
        returncode=completed.returncode,
        stdout=_bounded(completed.stdout, max_output_chars),
        stdout_truncated=len(completed.stdout) > max_output_chars,
        stderr=_bounded(completed.stderr),
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1:] or ["no stderr"]
        raise CommandError(
            f"{' '.join(arg_list[:3])} exited {result.returncode}: {detail[0]}", result
        )
    return result

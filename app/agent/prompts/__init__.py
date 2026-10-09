"""Prompt templates loaded from Markdown files (string.Template `$name` placeholders)."""

from functools import cache
from pathlib import Path
from string import Template

_PROMPT_DIR = Path(__file__).resolve().parent


@cache
def _template(name: str) -> Template:
    return Template((_PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8"))


def render(name: str, **values: object) -> str:
    return _template(name).substitute({k: str(v) for k, v in values.items()})

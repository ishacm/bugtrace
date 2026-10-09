"""Render deploy/cloud-run.service.yaml from environment variables.

Usage: uv run python scripts/render_cloud_run.py [OUTPUT]   (default var/deploy/service.yaml)
Every placeholder must be set; values are restricted to a safe character set so they
cannot inject YAML. Nothing secret is read or written.
"""

import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "deploy" / "cloud-run.service.yaml"
DEFAULT_OUTPUT = ROOT / "var" / "deploy" / "service.yaml"

PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
SAFE_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]*$")
REQUIRED = (
    "BUGTRACE_SERVICE",
    "BUGTRACE_IMAGE",
    "BUGTRACE_SERVICE_ACCOUNT",
    "BUGTRACE_MODEL_NAME",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
)


class RenderError(ValueError):
    pass


def render(template: str, env: Mapping[str, str]) -> str:
    names = set(PLACEHOLDER.findall(template))
    if names != set(REQUIRED):
        raise RenderError(f"template placeholders {sorted(names)} != {sorted(REQUIRED)}")
    missing = [name for name in REQUIRED if not env.get(name)]
    if missing:
        raise RenderError(f"missing environment variables: {', '.join(missing)}")
    unsafe = [name for name in REQUIRED if not SAFE_VALUE.match(env[name])]
    if unsafe:
        raise RenderError(f"unsafe characters in: {', '.join(unsafe)}")
    return PLACEHOLDER.sub(lambda match: env[match.group(1)], template)


def main(argv: list[str]) -> int:
    output = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUTPUT
    try:
        rendered = render(TEMPLATE.read_text(encoding="utf-8"), os.environ)
    except RenderError as exc:
        print(f"render_cloud_run: {exc}", file=sys.stderr)
        return 2
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

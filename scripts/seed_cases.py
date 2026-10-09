"""Seed agent-safe SWE-bench cases into a local JSONL file."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.data.source import DatasetFetchError  # noqa: E402
from app.data.store import write_cases  # noqa: E402
from app.data.swebench import load_public_cases  # noqa: E402


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=settings.seed_limit)
    parser.add_argument("--dataset", default=settings.swebench_dataset)
    parser.add_argument("--split", default=settings.swebench_split)
    parser.add_argument("--output", type=Path, default=settings.cases_path)
    args = parser.parse_args()

    try:
        cases = load_public_cases(args.dataset, args.split, args.limit)
    except DatasetFetchError as exc:
        print(f"seed: {exc}", file=sys.stderr)
        return 1
    count = write_cases(args.output, cases)
    print(f"seed: wrote {count} public cases from {args.dataset} to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

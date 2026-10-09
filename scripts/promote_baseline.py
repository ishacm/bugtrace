"""Promote a produced evaluation report to the stored baseline for its mode.

Usage: python scripts/promote_baseline.py REPORT.json [BASELINE.json]
Only reports produced by a real run should be promoted; never hand-edit metrics.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.evaluation.report import RunReport, as_baseline, write_report  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print(__doc__, file=sys.stderr)
        return 2
    report = RunReport.model_validate_json(Path(sys.argv[1]).read_text(encoding="utf-8"))
    default = ROOT / "evals" / "baselines" / f"{report.mode}.json"
    target = Path(sys.argv[2]) if len(sys.argv) == 3 else default
    write_report(as_baseline(report), target)
    print(f"baseline ({report.mode}, {report.summary.cases} cases) -> {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Stand-in for `python -m swebench.harness.run_evaluation` in tests (no Docker).

Accepts the same CLI, reads the dataset/predictions files and writes reports in
the harness's layout. Behaviour is chosen by FAKE_HARNESS_MODE:
  grade (default)  resolved iff the prediction contains FAKE_HARNESS_FIX
  apply_fail       per-instance log says the patch did not apply; no report
  tests_timeout    test output carries the harness timeout marker; no report
  crash            exit 3 without writing anything
  hang             write a pid file, then sleep (for timeout/cancel tests)
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    for flag in (
        "--dataset_name",
        "--split",
        "--predictions_path",
        "--max_workers",
        "--run_id",
        "--timeout",
        "--report_dir",
    ):
        parser.add_argument(flag, required=True)
    parser.add_argument("--instance_ids", nargs="+", required=True)
    args = parser.parse_args()

    mode = os.environ.get("FAKE_HARNESS_MODE", "grade")
    Path("argv.json").write_text(json.dumps(sys.argv[1:]))
    if mode == "crash":
        return 3
    if mode == "hang":
        Path("harness.pid").write_text(str(os.getpid()))
        time.sleep(120)
        return 0

    rows = {r["instance_id"]: r for r in json.loads(Path(args.dataset_name).read_text())}
    preds = [json.loads(line) for line in Path(args.predictions_path).read_text().splitlines()]
    summary: dict[str, list[str]] = {"resolved_ids": [], "unresolved_ids": [], "error_ids": []}
    for pred in preds:
        iid = pred["instance_id"]
        row = rows[iid]
        log_dir = Path("logs/run_evaluation") / args.run_id / pred["model_name_or_path"] / iid
        log_dir.mkdir(parents=True, exist_ok=True)
        if mode == "apply_fail":
            (log_dir / "run_instance.log").write_text(">>>>> Patch Apply Failed:\nerror\n")
            summary["error_ids"].append(iid)
            continue
        if mode == "tests_timeout":
            (log_dir / "test_output.txt").write_text("...\n\nTimeout error: 5 seconds exceeded.")
            summary["error_ids"].append(iid)
            continue
        resolved = os.environ.get("FAKE_HARNESS_FIX", "\x00") in pred["model_patch"]
        f2p = json.loads(row["FAIL_TO_PASS"])
        p2p = json.loads(row["PASS_TO_PASS"])
        report = {
            iid: {
                "patch_is_None": False,
                "patch_exists": True,
                "patch_successfully_applied": True,
                "resolved": resolved,
                "tests_status": {
                    "FAIL_TO_PASS": {
                        "success": f2p if resolved else [],
                        "failure": [] if resolved else f2p,
                    },
                    "PASS_TO_PASS": {"success": p2p, "failure": []},
                },
            }
        }
        (log_dir / "report.json").write_text(json.dumps(report))
        summary["resolved_ids" if resolved else "unresolved_ids"].append(iid)

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    name = preds[0]["model_name_or_path"].replace("/", "__")
    (report_dir / f"{name}.{args.run_id}.json").write_text(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Run a BugTrace investigation for one seeded case and wait for it to finish.

Uses the same service and SQLite store as the API, so CLI runs are visible via
`GET /api/investigations/{id}`; the record is also exported as JSON.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.runner import write_record  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.data.store import read_cases  # noqa: E402
from app.services.investigations import CaseNotFoundError, InvestigationService  # noqa: E402


async def investigate(instance_id: str | None, investigation_id: str | None) -> int:
    settings = get_settings()
    cases = read_cases(settings.cases_path)
    if not cases:
        print("investigate: no seeded cases; run `make seed` first", file=sys.stderr)
        return 1
    service = InvestigationService.from_settings(settings)
    try:
        stored = service.start(instance_id or cases[0].instance_id, investigation_id)
    except CaseNotFoundError:
        print(f"investigate: unknown instance_id {instance_id!r}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"investigate: {exc}", file=sys.stderr)
        return 1

    print(f"CASE OPENED {stored.case.instance_id} investigation={stored.investigation_id}")
    stored = await service.wait(stored.investigation_id)
    print(f"status={stored.status}")
    if stored.error:
        print(f"investigate: {stored.error} (see .env.example)", file=sys.stderr)
    record = stored.record
    if record is None or record.final_report is None:
        return 1
    path = write_record(record, settings.investigations_dir)
    report = record.final_report
    print(f"report={report.status} confidence={report.confidence} "
          f"tool_calls={report.tool_calls} iterations={report.iterations} "
          f"limits={report.limits_reached or '-'}")  # fmt: skip
    for suspect in report.suspects[:5]:
        print(f"  #{suspect.rank} {suspect.path} {suspect.symbol or ''} score={suspect.score}")
    print(f"ROOT CAUSE: {report.summary}")
    for claim in report.claims:
        print(f"  - {claim.statement} {claim.evidence_ids}")
    for error in record.errors:
        print(f"  ! {error}")
    if record.proposed_patch:
        patch = record.proposed_patch
        print(f"PATCH PROPOSED {patch.patch_id} {patch.affected_files}: {patch.summary}")
        print("  approve or reject in the UI / POST /api/investigations/{id}/decision")
    elif record.patch_error:
        print(f"NO VALID PATCH: {record.patch_error}")
    print(f"record: {path} (db: {settings.database_path})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instance_id", nargs="?")
    parser.add_argument("--investigation-id", default=None)
    args = parser.parse_args()
    return asyncio.run(investigate(args.instance_id, args.investigation_id))


if __name__ == "__main__":
    sys.exit(main())

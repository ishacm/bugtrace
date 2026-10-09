"""Live agent evaluation over a small regression manifest (manual / workflow_dispatch).

Runs each case through the real service (workspace + MCP tools + configured model),
then writes a JSON report with metrics read from the persisted investigations.
Needs GEMINI_API_KEY; with --approve-patches it also needs Docker for SWE-bench.

--approve-patches is an explicit human approval given when the run is started: each
proposed patch is approved through the normal decision path (sha256-bound, recorded
in the store), applied only in its own workspace, then graded by the evaluator.
Without it, proposals stay AWAITING_APPROVAL and nothing is applied.

Exit codes: 0 ok, 1 regression vs baseline (with --fail-on-regression),
2 unauthorized workspace write (always).
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.runner import write_record  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.data.store import read_cases  # noqa: E402
from app.evaluation.gold import GoldSource  # noqa: E402
from app.evaluation.report import (  # noqa: E402
    CaseReport,
    build_report,
    case_report,
    exit_code,
    load_manifest,
    reference_localization,
    report_digest,
    write_report,
)
from app.services.investigations import InvestigationService, LocalBackend  # noqa: E402
from app.storage.models import (  # noqa: E402
    TERMINAL_STATUSES,
    ApprovalDecision,
    InvestigationStatus,
)

S = InvestigationStatus
ROOT = Path(__file__).resolve().parent.parent


async def _settle(
    service: InvestigationService, investigation_id: str, timeout: float
) -> InvestigationStatus:
    """Wait for the background run; cancel it if it exceeds the per-case budget."""
    try:
        await asyncio.wait_for(service.wait(investigation_id), timeout)
    except TimeoutError:
        if service.get(investigation_id).status not in TERMINAL_STATUSES:
            service.cancel(investigation_id)
            await service.wait(investigation_id)
    return service.get(investigation_id).status


async def run_case(
    service: InvestigationService,
    backend: LocalBackend,
    gold: GoldSource,
    instance_id: str,
    *,
    approve: bool,
    timeout: float,
    records_dir: Path,
) -> CaseReport:
    stored = service.start(instance_id)
    investigation_id = stored.investigation_id
    print(f"CASE OPENED {instance_id} investigation={investigation_id}", flush=True)
    status = await _settle(service, investigation_id, timeout)
    if status == S.AWAITING_APPROVAL and approve:
        service.decide(investigation_id, ApprovalDecision.APPROVE)
        status = await _settle(service, investigation_id, timeout)
    final = service.get(investigation_id)
    try:
        changed = await backend.changed_files(investigation_id)
    except Exception as exc:  # workspace missing (e.g. clone failed): nothing changed
        print(f"  workspace not inspected: {type(exc).__name__}", flush=True)
        changed = []
    # Reference data is read only now, after the agent has finished with the case.
    localization = await asyncio.to_thread(reference_localization, gold, final)
    if final.record is not None:
        write_record(final.record, records_dir)
    report = case_report(final, changed, localization)
    print(
        f"  status={report.status} report={report.report_status} "
        f"evaluation={report.evaluation_status} unauthorized_writes={report.unauthorized_writes}",
        flush=True,
    )
    return report


async def main_async(args: argparse.Namespace) -> int:
    settings = get_settings()
    if settings.gemini_api_key is None or not settings.gemini_api_key.get_secret_value():
        print("agent_eval: GEMINI_API_KEY is not set", file=sys.stderr)
        return 3
    manifest = load_manifest(args.manifest)
    seeded = {c.instance_id for c in read_cases(settings.cases_path)}
    missing = [c.instance_id for c in manifest.cases if c.instance_id not in seeded]
    if missing:
        print(f"agent_eval: cases not seeded: {missing}", file=sys.stderr)
        return 3

    backend = LocalBackend(settings)
    service = InvestigationService.from_settings(settings, backend)
    root = settings.evaluation_dir.resolve()
    gold = GoldSource(root / "reference", settings.evaluation_dataset, settings.evaluation_split)
    cases = []
    for case in manifest.cases:
        cases.append(
            await run_case(
                service,
                backend,
                gold,
                case.instance_id,
                approve=args.approve_patches,
                timeout=args.case_timeout,
                records_dir=settings.investigations_dir,
            )
        )
    await service.shutdown()

    report = build_report(
        "live",
        cases,
        config={
            "manifest": str(args.manifest),
            "model_name": settings.model_name,
            "max_iterations": settings.agent_max_iterations,
            "max_tool_calls": settings.agent_max_tool_calls,
            "approve_patches": args.approve_patches,
            "harness_version": settings.evaluation_harness_version,
            "git_sha": os.environ.get("GITHUB_SHA"),
        },
        baseline_path=args.baseline,
    )
    write_report(report, args.output)
    print(report_digest(report))
    print(f"report: {args.output}")
    return exit_code(report, fail_on_regression=args.fail_on_regression)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "evals" / "cases.json")
    parser.add_argument("--output", type=Path, default=ROOT / "var" / "evals" / "report.json")
    parser.add_argument("--baseline", type=Path, default=ROOT / "evals" / "baselines" / "live.json")
    parser.add_argument("--approve-patches", action="store_true")
    parser.add_argument("--fail-on-regression", action="store_true")
    parser.add_argument("--case-timeout", type=float, default=1800)
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())

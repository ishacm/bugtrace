"""Runs investigations and records their lifecycle in the store.

Two execution modes share the graph, the store and the lifecycle rules:
- background (local, Cloud Run): each run is an in-process asyncio task.
- stepwise (Vercel): nothing runs after a response is sent. Each `advance()` call
  holds a lease, runs a bounded slice of the graph from its durable checkpoint,
  persists it and returns, so any process can take the next step.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver

from app.agent.model_client import ModelClient, build_model_client
from app.agent.models import AgentEvent
from app.agent.runner import (
    advance_investigation,
    open_checkpointer,
    resume_investigation,
    run_investigation,
    sqlite_checkpointer,
    to_record,
)
from app.agent.state import InvestigationState
from app.agent.tools import PatchApplier, RepoToolClient
from app.config import Settings
from app.data.models import PublicCase
from app.data.store import read_cases
from app.evaluation.boundary import (
    DisabledEvaluator,
    EvaluationRefusedError,
    Evaluator,
    build_evaluator,
    build_request,
    failed_result,
)
from app.evaluation.metrics import trace_metrics
from app.evaluation.models import EvaluationResult, EvaluationStatus
from app.mcp_servers.client import PatchMCPClient, RepoMCPClient
from app.mcp_servers.models import (
    CheckPatchResult,
    FindSymbolResult,
    GitLogResult,
    ReadFileResult,
    SearchCodeResult,
)
from app.storage import open_store
from app.storage.models import (
    RUNNING_STATUSES,
    TERMINAL_STATUSES,
    Approval,
    ApprovalDecision,
    InvalidTransitionError,
    InvestigationStatus,
    StoredInvestigation,
)
from app.storage.sqlite import InvestigationStore
from app.workspace.manager import WorkspaceManager, new_investigation_id

logger = logging.getLogger(__name__)

VERCEL_WORKSPACE_MAX_AGE = 3600.0  # seconds; /tmp is small and per instance

S = InvestigationStatus


class CaseNotFoundError(LookupError):
    pass


class StepwiseDisabledError(RuntimeError):
    """`advance()` was called on a service running investigations in the background."""


class _RunStopped(Exception):  # noqa: N818
    """The run's status moved underneath a step (e.g. cancelled); stop without failing."""


@dataclass(frozen=True)
class StepPolicy:
    """Bounds for one stepwise `advance()` (see Settings.step_*)."""

    budget_seconds: float
    timeout_seconds: float
    lease_seconds: float
    max_failures: int


StepResult = Literal["advanced", "idle", "busy", "retry", "failed"]


@dataclass(frozen=True)
class StepOutcome:
    stored: StoredInvestigation
    result: StepResult
    pending: bool  # whether another advance has work to do


class _NoRepository:
    """Repository client for resuming a rejected patch: that path never calls a tool."""

    async def _refuse(self, *_: Any, **__: Any) -> Any:
        raise RuntimeError("no repository access after a rejected patch")

    async def search_code(self, *a: Any, **k: Any) -> SearchCodeResult:
        return await self._refuse()

    async def read_file(self, *a: Any, **k: Any) -> ReadFileResult:
        return await self._refuse()

    async def find_symbol(self, *a: Any, **k: Any) -> FindSymbolResult:
        return await self._refuse()

    async def git_log(self, *a: Any, **k: Any) -> GitLogResult:
        return await self._refuse()

    async def check_patch(self, *a: Any, **k: Any) -> CheckPatchResult:
        return await self._refuse()


class InvestigationBackend(Protocol):
    """What a run needs from the outside world; swapped for fakes in tests."""

    async def prepare(self, investigation_id: str, case: PublicCase) -> None: ...

    def model(self) -> ModelClient: ...

    def repo_client(self, investigation_id: str) -> AbstractAsyncContextManager[RepoToolClient]: ...

    def patch_client(self, investigation_id: str) -> AbstractAsyncContextManager[PatchApplier]: ...

    async def changed_files(self, investigation_id: str) -> list[str]: ...


class LocalBackend:
    """Real backend: git workspace + stdio MCP repo server + configured model."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.workspaces = settings.workspaces_dir.resolve()
        self.manager = WorkspaceManager(self.workspaces, git_timeout=settings.git_timeout_seconds)

    async def prepare(self, investigation_id: str, case: PublicCase) -> None:
        """Idempotent: reuses a workspace already at the base commit, else clones it.

        On Vercel the workspace lives in the instance's /tmp and may be gone at the next
        request; stepwise runs call this before every step and re-fetch when needed."""
        if self.settings.platform == "vercel":
            await asyncio.to_thread(self.manager.cleanup_stale, VERCEL_WORKSPACE_MAX_AGE)
        await asyncio.to_thread(self.manager.prepare, investigation_id, case)

    def model(self) -> ModelClient:
        return build_model_client(self.settings)

    @asynccontextmanager
    async def repo_client(self, investigation_id: str) -> AsyncIterator[RepoToolClient]:
        async with RepoMCPClient.connect(investigation_id, self.workspaces) as client:
            yield client

    @asynccontextmanager
    async def patch_client(self, investigation_id: str) -> AsyncIterator[PatchApplier]:
        """Privileged server; it re-checks the persisted approval before every apply."""
        url = self.settings.database_url
        async with PatchMCPClient.connect(
            investigation_id,
            self.workspaces,
            self.settings.database_path,
            database_url=url.get_secret_value()
            if url and self.settings.storage_backend == "postgres"
            else None,
        ) as client:
            yield client

    async def changed_files(self, investigation_id: str) -> list[str]:
        return await asyncio.to_thread(self.manager.changed_files, investigation_id)


class InvestigationService:
    def __init__(
        self,
        *,
        store: InvestigationStore,
        backend: InvestigationBackend,
        cases_path: Path,
        max_iterations: int,
        max_tool_calls: int,
        checkpoint_path: Path,
        evaluator: Evaluator,
        max_patch_attempts: int = 2,
        checkpointer: Callable[[], AbstractAsyncContextManager[BaseCheckpointSaver]] | None = None,
        step_policy: StepPolicy | None = None,
    ) -> None:
        self.store = store
        self.backend = backend
        self.cases_path = cases_path
        self.max_iterations = max_iterations
        self.max_tool_calls = max_tool_calls
        self.checkpoint_path = checkpoint_path
        self.max_patch_attempts = max_patch_attempts
        self.evaluator = evaluator
        self._checkpointer = checkpointer or (lambda: sqlite_checkpointer(self.checkpoint_path))
        # None: background mode. Set: stepwise mode (no background tasks at all).
        self.step_policy = step_policy
        self._tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def stepwise(self) -> bool:
        return self.step_policy is not None

    @property
    def evaluation_available(self) -> bool:
        return not isinstance(self.evaluator, DisabledEvaluator)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        backend: InvestigationBackend | None = None,
        evaluator: Evaluator | None = None,
    ) -> "InvestigationService":
        step_policy = None
        if settings.execution_mode == "stepwise":
            step_policy = StepPolicy(
                budget_seconds=settings.step_budget_seconds,
                timeout_seconds=settings.step_timeout_seconds,
                lease_seconds=settings.step_lease_seconds,
                max_failures=settings.step_max_failures,
            )
        return cls(
            store=open_store(settings),
            backend=backend or LocalBackend(settings),
            cases_path=settings.cases_path,
            max_iterations=settings.agent_max_iterations,
            max_tool_calls=settings.agent_max_tool_calls,
            checkpoint_path=settings.checkpoint_path,
            evaluator=evaluator or build_evaluator(settings),
            max_patch_attempts=settings.agent_max_patch_attempts,
            checkpointer=lambda: open_checkpointer(settings),
            step_policy=step_policy,
        )

    # --- public API -----------------------------------------------------------

    def start(self, instance_id: str, investigation_id: str | None = None) -> StoredInvestigation:
        """Create an investigation and schedule it; returns without waiting for the run.

        Stepwise mode schedules nothing: the run waits in PREPARING for `advance()`."""
        case = next((c for c in read_cases(self.cases_path) if c.instance_id == instance_id), None)
        if case is None:
            raise CaseNotFoundError(instance_id)
        investigation_id = investigation_id or new_investigation_id()
        if str(uuid.UUID(investigation_id)) != investigation_id:
            raise ValueError(f"investigation id must be a canonical UUID: {investigation_id!r}")
        self.store.create(investigation_id, case)
        stored = self.store.transition(investigation_id, S.PREPARING)
        if not self.stepwise:
            self._spawn(investigation_id, self._run(investigation_id, case))
        return stored

    def decide(self, investigation_id: str, decision: ApprovalDecision) -> StoredInvestigation:
        """Record a human decision on the proposed patch and resume the paused graph.

        Raises DecisionConflictError unless the investigation is AWAITING_APPROVAL."""
        if decision == ApprovalDecision.APPROVE:
            event = AgentEvent(
                type="patch_approved",
                title="PATCH APPROVED",
                message="Applying the approved patch inside the investigation workspace.",
            )
        else:
            event = AgentEvent(
                type="patch_rejected",
                title="PATCH REJECTED",
                message="The patch was rejected; the workspace was left unchanged.",
            )
        stored = self.store.record_decision(investigation_id, decision, event=event)
        approval = stored.approval
        if approval is None:
            raise RuntimeError("decision was not persisted")
        if not self.stepwise:  # stepwise: the next advance() resumes the graph
            self._spawn(investigation_id, self._resume(investigation_id, approval))
        return stored

    def get(self, investigation_id: str) -> StoredInvestigation:
        return self.store.get(investigation_id)

    async def wait(self, investigation_id: str) -> StoredInvestigation:
        """Await the background run (CLI use); the API polls instead."""
        task = self._tasks.get(investigation_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        return self.store.get(investigation_id)

    def cancel(self, investigation_id: str) -> StoredInvestigation:
        stored = self.store.transition(investigation_id, S.CANCELLED)
        task = self._tasks.get(investigation_id)
        if task is not None:
            task.cancel()
        return stored

    def recover(self) -> list[str]:
        """Fail runs whose background task died with a previous server process.

        Stepwise runs have no background task: a new process simply continues them."""
        if self.stepwise:
            return []
        interrupted = self.store.ids_with_status(RUNNING_STATUSES)
        for investigation_id in interrupted:
            self._fail(investigation_id, "interrupted by server restart")
        return interrupted

    async def shutdown(self) -> None:
        tasks = list(self._tasks.items())
        for investigation_id, task in tasks:
            self._fail(investigation_id, "interrupted by server shutdown")
            task.cancel()
        await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)

    # --- background run -------------------------------------------------------

    def _spawn(self, investigation_id: str, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(work)
        self._tasks[investigation_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(investigation_id, None))

    async def _persist(self, state: InvestigationState) -> None:
        self.store.save_record(to_record(state))

    async def _run(self, investigation_id: str, case: PublicCase) -> None:
        try:
            await self.backend.prepare(investigation_id, case)
            self.store.append_event(
                investigation_id,
                AgentEvent(
                    type="repo_ready",
                    title="REPO READY",
                    message=f"{case.repo} @ {case.base_commit[:12]}",
                ),
            )
            self.store.transition(investigation_id, S.INVESTIGATING)
            model = self.backend.model()
            async with (
                self._checkpointer() as saver,
                self.backend.repo_client(investigation_id) as client,
            ):
                state = await run_investigation(
                    case=case,
                    investigation_id=investigation_id,
                    model=model,
                    repo_client=client,
                    max_iterations=self.max_iterations,
                    max_tool_calls=self.max_tool_calls,
                    max_patch_attempts=self.max_patch_attempts,
                    checkpointer=saver,
                    on_update=self._persist,
                )
            self.store.save_record(to_record(state))
            self._settle(investigation_id, state)
        except InvalidTransitionError as exc:
            # Status moved underneath the run (e.g. cancelled); keep the stored status.
            logger.info("investigation %s stopped: %s", investigation_id, exc)
        except Exception as exc:
            logger.exception("investigation %s failed", investigation_id)
            self._fail(investigation_id, f"{type(exc).__name__}: {exc}")

    def _settle(self, investigation_id: str, state: InvestigationState) -> None:
        """Move a run whose graph ended or paused to its next status."""
        if state.get("final_report") is None:
            self._fail(investigation_id, "agent produced no final report")
            return
        patch = state.get("proposed_patch")
        if state.get("approval_status") == "pending" and patch is not None:
            # The graph is paused at its approval interrupt; nothing has been applied.
            self.store.transition(
                investigation_id,
                S.AWAITING_APPROVAL,
                event=AgentEvent(
                    type="approval_required",
                    title="APPROVAL REQUIRED",
                    message=f"Review {patch.patch_id} ({', '.join(patch.affected_files)}). "
                    "The repository is unchanged until you approve.",
                ),
            )
            return
        # No patch (no COMPLETED root cause, or no valid diff): the case stays open.
        self.store.transition(investigation_id, S.UNRESOLVED)

    async def _resume(self, investigation_id: str, approval: Approval) -> None:
        approve = approval.decision == ApprovalDecision.APPROVE
        try:
            async with AsyncExitStack() as stack:
                saver = await stack.enter_async_context(self._checkpointer())
                client = await stack.enter_async_context(self.backend.repo_client(investigation_id))
                # The privileged server is only started for an approved patch.
                patcher = (
                    await stack.enter_async_context(self.backend.patch_client(investigation_id))
                    if approve
                    else None
                )
                state = await resume_investigation(
                    investigation_id=investigation_id,
                    decision=approval.decision.value,
                    patch_sha256=approval.patch_sha256,
                    model=self.backend.model(),
                    repo_client=client,
                    patcher=patcher,
                    checkpointer=saver,
                    max_iterations=self.max_iterations,
                    max_tool_calls=self.max_tool_calls,
                    on_update=self._persist,
                )
            await self._after_resume(investigation_id, approve, state)
        except InvalidTransitionError as exc:
            logger.info("investigation %s stopped: %s", investigation_id, exc)
        except Exception as exc:
            logger.exception("investigation %s failed after decision", investigation_id)
            self._fail(investigation_id, f"{type(exc).__name__}: {exc}")

    async def _after_resume(
        self, investigation_id: str, approve: bool, state: InvestigationState
    ) -> None:
        self.store.save_record(to_record(state))
        if not approve:
            return
        result = state.get("patch_result")
        if result is None or not result.applied:
            # Nothing was applied, so there is nothing to evaluate.
            self.store.transition(investigation_id, S.UNRESOLVED)
            return
        await self._evaluate(investigation_id)

    async def _evaluate(self, investigation_id: str, *, started_already: bool = False) -> None:
        """Grade the applied patch with the SWE-bench evaluator and close the case.

        Only the gold-free result is persisted; nothing flows back into agent state.
        `started_already`: a stepwise retry after a step died while EVALUATING."""
        if started_already:
            stored = self.store.get(investigation_id)
        else:
            stored = self.store.transition(
                investigation_id,
                S.EVALUATING,
                event=AgentEvent(
                    type="evaluation_started",
                    title="EVALUATION STARTED",
                    message="Running the SWE-bench tests against the applied patch "
                    "in an isolated container."
                    if not isinstance(self.evaluator, DisabledEvaluator)
                    else "Checking whether benchmark testing is available.",
                ),
            )
        try:
            request = build_request(stored, await self.backend.changed_files(investigation_id))
        except EvaluationRefusedError as exc:
            self._fail(investigation_id, f"evaluation refused: {exc}")
            return
        started = datetime.now(UTC)
        try:
            result = await self.evaluator.evaluate(request)
        except Exception as exc:
            logger.exception("evaluation of %s failed", investigation_id)
            result = failed_result(
                request,
                EvaluationStatus.ERROR,
                "INTERNAL_ERROR",
                f"The evaluator failed unexpectedly ({type(exc).__name__}).",
                started,
            )
        result = result.model_copy(update={"trace": trace_metrics(stored)})
        self.store.record_evaluation(investigation_id, result, event=_evaluation_event(result))

    # --- stepwise execution ----------------------------------------------------

    def needs_advance(self, stored: StoredInvestigation) -> bool:
        """Whether an `advance()` has work for this investigation."""
        if not self.stepwise:
            return False
        return self._step_work(stored) is not None

    async def advance(self, investigation_id: str) -> StepOutcome:
        """Run one bounded, leased step of a stepwise investigation.

        Safe to call at any time, from any process, any number of times: it does nothing
        while the run is awaiting a decision or finished ("idle"), or while another
        request holds the step lease ("busy"). A step that raises or exceeds
        `timeout_seconds` is retried by a later call; after `max_failures` consecutive
        failed (or abandoned) steps the investigation is marked FAILED."""
        policy = self.step_policy
        if policy is None:
            raise StepwiseDisabledError("investigations run in the background in this mode")
        stored = self.store.get(investigation_id)
        if self._step_work(stored) is None:
            return StepOutcome(stored, "idle", pending=False)
        owner = str(uuid.uuid4())
        lease = self.store.acquire_step(investigation_id, owner, policy.lease_seconds)
        if lease is None:
            return StepOutcome(self.store.get(investigation_id), "busy", pending=True)
        failed, reason = False, ""
        try:
            if lease.failures >= policy.max_failures:
                # The previous holder died mid-step (abandoned lease) one time too many.
                self._fail(investigation_id, _too_many_failures(lease.failures))
                return StepOutcome(self.store.get(investigation_id), "failed", pending=False)
            work = self._step_work(self.store.get(investigation_id))  # re-read under the lease
            if work is None:
                return StepOutcome(self.store.get(investigation_id), "idle", pending=False)
            async with asyncio.timeout(policy.timeout_seconds):
                await work()
        except (_RunStopped, InvalidTransitionError) as exc:
            logger.info("investigation %s step stopped: %s", investigation_id, exc)
        except Exception as exc:
            failed = True
            reason = "step timed out" if isinstance(exc, TimeoutError) else type(exc).__name__
            logger.warning("investigation %s step failed: %s", investigation_id, reason)
        finally:
            failures = self.store.release_step(investigation_id, owner, failed=failed)
        if failed:
            if failures >= policy.max_failures:
                self._fail(investigation_id, _too_many_failures(failures, reason))
                return StepOutcome(self.store.get(investigation_id), "failed", pending=False)
            self.store.append_event(
                investigation_id,
                AgentEvent(
                    type="error",
                    title="STEP RETRY",
                    message=f"A step did not finish ({reason}); it will be retried "
                    f"from the last checkpoint ({failures}/{policy.max_failures}).",
                ),
            )
        stored = self.store.get(investigation_id)
        return StepOutcome(
            stored, "retry" if failed else "advanced", pending=self.needs_advance(stored)
        )

    def _step_work(
        self, stored: StoredInvestigation
    ) -> Callable[[], Coroutine[Any, Any, None]] | None:
        """The next step for this investigation, or None when nothing can run now."""
        status = stored.status
        approval = stored.approval
        if status in (S.CREATED, S.PREPARING, S.INVESTIGATING):
            return lambda: self._investigate_step(stored)
        if status == S.PATCHING and approval is not None:
            return lambda: self._resume_step(stored, approval)
        if status == S.EVALUATING:
            return lambda: self._evaluate(stored.investigation_id, started_already=True)
        record = stored.record
        if (
            status == S.UNRESOLVED
            and approval is not None
            and approval.decision == ApprovalDecision.REJECT
            and record is not None
            and record.approval_status == "pending"
        ):
            # Rejected while paused: resume once so the graph records the rejection.
            return lambda: self._resume_step(stored, approval)
        return None

    def _live_persist(self, investigation_id: str) -> Callable[[InvestigationState], Any]:
        async def persist(state: InvestigationState) -> None:
            if self.store.get(investigation_id).status in TERMINAL_STATUSES:
                raise _RunStopped("investigation finished or was cancelled during a step")
            self.store.save_record(to_record(state))

        return persist

    async def _investigate_step(self, stored: StoredInvestigation) -> None:
        policy = self.step_policy
        assert policy is not None  # noqa: S101 (only reachable in stepwise mode)
        investigation_id, case = stored.investigation_id, stored.case
        if stored.status == S.CREATED:
            self.store.transition(investigation_id, S.PREPARING)
        # Re-created whenever this instance does not have it (ephemeral filesystem).
        await self.backend.prepare(investigation_id, case)
        if stored.status != S.INVESTIGATING:
            self.store.transition(
                investigation_id,
                S.INVESTIGATING,
                event=AgentEvent(
                    type="repo_ready",
                    title="REPO READY",
                    message=f"{case.repo} @ {case.base_commit[:12]}",
                ),
            )
        model = self.backend.model()
        async with (
            self._checkpointer() as saver,
            self.backend.repo_client(investigation_id) as client,
        ):
            step = await advance_investigation(
                case=case,
                investigation_id=investigation_id,
                model=model,
                repo_client=client,
                checkpointer=saver,
                max_iterations=self.max_iterations,
                max_tool_calls=self.max_tool_calls,
                max_patch_attempts=self.max_patch_attempts,
                budget_seconds=policy.budget_seconds,
                on_update=self._live_persist(investigation_id),
            )
        self.store.save_record(to_record(step.state))
        if step.settled:
            self._settle(investigation_id, step.state)

    async def _resume_step(self, stored: StoredInvestigation, approval: Approval) -> None:
        investigation_id = stored.investigation_id
        approve = approval.decision == ApprovalDecision.APPROVE
        async with AsyncExitStack() as stack:
            saver = await stack.enter_async_context(self._checkpointer())
            client: RepoToolClient = _NoRepository()
            patcher = None
            if approve:
                await self.backend.prepare(investigation_id, stored.case)
                client = await stack.enter_async_context(self.backend.repo_client(investigation_id))
                patcher = await stack.enter_async_context(
                    self.backend.patch_client(investigation_id)
                )
            state = await resume_investigation(
                investigation_id=investigation_id,
                decision=approval.decision.value,
                patch_sha256=approval.patch_sha256,
                model=self.backend.model(),
                repo_client=client,
                patcher=patcher,
                checkpointer=saver,
                max_iterations=self.max_iterations,
                max_tool_calls=self.max_tool_calls,
                allow_finished=True,  # a previous step resumed it, then died
            )
        await self._after_resume(investigation_id, approve, state)

    def _fail(self, investigation_id: str, message: str) -> None:
        event = AgentEvent(type="error", title="INVESTIGATION FAILED", message=message)
        with suppress(InvalidTransitionError):
            self.store.transition(investigation_id, S.FAILED, error=message, event=event)


def _too_many_failures(count: int, reason: str = "") -> str:
    detail = f"; last: {reason}" if reason else ""
    return f"stopped after {count} failed or interrupted steps{detail}"


_EVALUATION_TITLES = {
    EvaluationStatus.RESOLVED: "CASE CLOSED",
    EvaluationStatus.UNRESOLVED: "TESTS FAILED",
    EvaluationStatus.ERROR: "EVALUATION ERROR",
    EvaluationStatus.TIMEOUT: "EVALUATION TIMED OUT",
    EvaluationStatus.UNAVAILABLE: "EVALUATOR UNAVAILABLE",
}


def _evaluation_event(result: EvaluationResult) -> AgentEvent:
    message = result.message
    if result.target_tests is not None:
        target = result.target_tests
        message += f" Target tests: {target.passed}/{target.passed + target.failed} passing."
    if result.regression_tests is not None:
        regression = result.regression_tests
        message += (
            f" Regression tests: {regression.passed}/"
            f"{regression.passed + regression.failed} passing."
        )
    return AgentEvent(
        type="case_closed" if result.status == EvaluationStatus.RESOLVED else "evaluation_finished",
        title=_EVALUATION_TITLES[result.status],
        message=message,
        metadata={"evaluation_status": result.status.value},
    )

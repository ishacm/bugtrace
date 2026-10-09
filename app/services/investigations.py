"""Runs investigations in the background and records their lifecycle in the store."""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Coroutine
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from app.agent.model_client import ModelClient, build_model_client
from app.agent.models import AgentEvent
from app.agent.runner import (
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
from app.evaluation.evaluator import (
    EvaluationRefusedError,
    Evaluator,
    build_evaluator,
    build_request,
    failed_result,
)
from app.evaluation.metrics import trace_metrics
from app.evaluation.models import EvaluationResult, EvaluationStatus
from app.mcp_servers.client import PatchMCPClient, RepoMCPClient
from app.storage.models import (
    RUNNING_STATUSES,
    Approval,
    ApprovalDecision,
    InvalidTransitionError,
    InvestigationStatus,
    StoredInvestigation,
)
from app.storage.sqlite import InvestigationStore
from app.workspace.manager import WorkspaceManager, new_investigation_id

logger = logging.getLogger(__name__)

S = InvestigationStatus


class CaseNotFoundError(LookupError):
    pass


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
        database = self.settings.database_path
        async with PatchMCPClient.connect(investigation_id, self.workspaces, database) as client:
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
    ) -> None:
        self.store = store
        self.backend = backend
        self.cases_path = cases_path
        self.max_iterations = max_iterations
        self.max_tool_calls = max_tool_calls
        self.checkpoint_path = checkpoint_path
        self.max_patch_attempts = max_patch_attempts
        self.evaluator = evaluator
        self._tasks: dict[str, asyncio.Task[None]] = {}

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        backend: InvestigationBackend | None = None,
        evaluator: Evaluator | None = None,
    ) -> "InvestigationService":
        return cls(
            store=InvestigationStore(settings.database_path),
            backend=backend or LocalBackend(settings),
            cases_path=settings.cases_path,
            max_iterations=settings.agent_max_iterations,
            max_tool_calls=settings.agent_max_tool_calls,
            checkpoint_path=settings.checkpoint_path,
            evaluator=evaluator or build_evaluator(settings),
            max_patch_attempts=settings.agent_max_patch_attempts,
        )

    # --- public API -----------------------------------------------------------

    def start(self, instance_id: str, investigation_id: str | None = None) -> StoredInvestigation:
        """Create an investigation and schedule it; returns without waiting for the run."""
        case = next((c for c in read_cases(self.cases_path) if c.instance_id == instance_id), None)
        if case is None:
            raise CaseNotFoundError(instance_id)
        investigation_id = investigation_id or new_investigation_id()
        if str(uuid.UUID(investigation_id)) != investigation_id:
            raise ValueError(f"investigation id must be a canonical UUID: {investigation_id!r}")
        self.store.create(investigation_id, case)
        stored = self.store.transition(investigation_id, S.PREPARING)
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
        """Fail runs whose background task died with a previous server process."""
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
                sqlite_checkpointer(self.checkpoint_path) as saver,
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
        except InvalidTransitionError as exc:
            # Status moved underneath the run (e.g. cancelled); keep the stored status.
            logger.info("investigation %s stopped: %s", investigation_id, exc)
        except Exception as exc:
            logger.exception("investigation %s failed", investigation_id)
            self._fail(investigation_id, f"{type(exc).__name__}: {exc}")

    async def _resume(self, investigation_id: str, approval: Approval) -> None:
        approve = approval.decision == ApprovalDecision.APPROVE
        try:
            async with AsyncExitStack() as stack:
                saver = await stack.enter_async_context(sqlite_checkpointer(self.checkpoint_path))
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
            self.store.save_record(to_record(state))
            if not approve:
                return
            result = state.get("patch_result")
            if result is None or not result.applied:
                # Nothing was applied, so there is nothing to evaluate.
                self.store.transition(investigation_id, S.UNRESOLVED)
                return
            await self._evaluate(investigation_id)
        except InvalidTransitionError as exc:
            logger.info("investigation %s stopped: %s", investigation_id, exc)
        except Exception as exc:
            logger.exception("investigation %s failed after decision", investigation_id)
            self._fail(investigation_id, f"{type(exc).__name__}: {exc}")

    async def _evaluate(self, investigation_id: str) -> None:
        """Grade the applied patch with the SWE-bench evaluator and close the case.

        Only the gold-free result is persisted; nothing flows back into agent state."""
        stored = self.store.transition(
            investigation_id,
            S.EVALUATING,
            event=AgentEvent(
                type="evaluation_started",
                title="EVALUATION STARTED",
                message="Running the SWE-bench tests against the applied patch "
                "in an isolated container.",
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

    def _fail(self, investigation_id: str, message: str) -> None:
        event = AgentEvent(type="error", title="INVESTIGATION FAILED", message=message)
        with suppress(InvalidTransitionError):
            self.store.transition(investigation_id, S.FAILED, error=message, event=event)


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

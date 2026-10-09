"""Run one investigation through the graph and persist a JSON record of it.

The graph is checkpointed per investigation (thread id = investigation id). A run
that proposes a patch stops at the approval interrupt; `resume_investigation`
continues it, possibly in a later process, from the durable SQLite checkpoint.
"""

import enum
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

import aiosqlite
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from pydantic import BaseModel

from app.agent import models as agent_models
from app.agent import state as agent_state
from app.agent.graph import build_graph, initial_state, recursion_limit
from app.agent.model_client import ModelClient
from app.agent.models import (
    AgentEvent,
    ApprovalStatus,
    Evidence,
    FinalReport,
    Hypothesis,
    InvestigationPlan,
    IssueAnalysis,
    PatchResult,
    ProposedPatch,
    Suspect,
)
from app.agent.state import InvestigationState
from app.agent.tools import PatchApplier, RepoToolClient, ToolExecutor
from app.data import models as data_models
from app.data.models import PublicCase

APPROVAL_NODE = "pause_for_approval"


class NotAwaitingApprovalError(RuntimeError):
    pass


def _state_types(*modules: ModuleType) -> list[tuple[str, str]]:
    return [
        (module.__name__, name)
        for module in modules
        for name, obj in vars(module).items()
        if isinstance(obj, type)
        and obj.__module__ == module.__name__
        and issubclass(obj, BaseModel | enum.Enum)
    ]


def checkpoint_serde() -> JsonPlusSerializer:
    """Serializer that only revives BugTrace's own state types from checkpoints."""
    allowed = _state_types(agent_models, agent_state, data_models)
    return JsonPlusSerializer(allowed_msgpack_modules=allowed)


@asynccontextmanager
async def sqlite_checkpointer(path: Path) -> AsyncIterator[AsyncSqliteSaver]:
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(str(path)) as conn:
        saver = AsyncSqliteSaver(conn, serde=checkpoint_serde())
        await saver.setup()
        yield saver


class InvestigationRecord(BaseModel):
    """Serializable snapshot of a finished run, for the later API/UI."""

    investigation_id: str
    case: PublicCase
    analysis: IssueAnalysis | None
    plan: InvestigationPlan | None
    evidence: list[Evidence]
    suspects: list[Suspect]
    hypotheses: list[Hypothesis]
    searches_performed: list[str]
    files_inspected: list[str]
    tool_call_count: int
    iteration: int
    limits_reached: list[str]
    errors: list[str]
    events: list[AgentEvent]
    final_report: FinalReport | None
    # V2 patch stage; defaults keep V1 records readable.
    proposed_patch: ProposedPatch | None = None
    patch_error: str | None = None
    approval_status: ApprovalStatus | None = None
    patch_result: PatchResult | None = None


async def run_investigation(
    *,
    case: PublicCase,
    investigation_id: str,
    model: ModelClient,
    repo_client: RepoToolClient,
    max_iterations: int,
    max_tool_calls: int,
    max_patch_attempts: int = 2,
    checkpointer: BaseCheckpointSaver | None = None,
    on_update: Callable[[InvestigationState], Awaitable[None]] | None = None,
) -> InvestigationState:
    """Run until the graph ends or pauses for approval (`approval_status == "pending"`).

    `on_update` sees the full state after every node."""
    executor = ToolExecutor(repo_client, max_calls=max_tool_calls)
    graph = build_graph(
        model, executor, checkpointer=checkpointer or InMemorySaver(serde=checkpoint_serde())
    )
    state = initial_state(
        investigation_id=investigation_id,
        case=case,
        max_iterations=max_iterations,
        max_tool_calls=max_tool_calls,
        max_patch_attempts=max_patch_attempts,
    )
    return await _drive(graph, state, _config(investigation_id, max_iterations), state, on_update)


async def resume_investigation(
    *,
    investigation_id: str,
    decision: Literal["approve", "reject"],
    patch_sha256: str,
    model: ModelClient,
    repo_client: RepoToolClient,
    patcher: PatchApplier | None,
    checkpointer: BaseCheckpointSaver,
    max_iterations: int,
    max_tool_calls: int,
    on_update: Callable[[InvestigationState], Awaitable[None]] | None = None,
) -> InvestigationState:
    """Continue a run paused at the approval interrupt with a human decision.

    The decision is bound to the patch's sha256; any mismatch resumes as a rejection."""
    executor = ToolExecutor(repo_client, max_calls=max_tool_calls)
    graph = build_graph(model, executor, patcher=patcher, checkpointer=checkpointer)
    config = _config(investigation_id, max_iterations)
    snapshot = await graph.aget_state(config)
    if APPROVAL_NODE not in snapshot.next:
        raise NotAwaitingApprovalError(f"investigation {investigation_id} is not paused")
    current = InvestigationState(**snapshot.values)
    command = Command(resume={"decision": decision, "sha256": patch_sha256})
    return await _drive(graph, command, config, current, on_update)


def _config(investigation_id: str, max_iterations: int) -> dict[str, Any]:
    return {
        "recursion_limit": recursion_limit(max_iterations),
        "configurable": {"thread_id": investigation_id},
    }


async def _drive(
    graph: CompiledStateGraph,
    graph_input: Any,
    config: dict[str, Any],
    current: InvestigationState,
    on_update: Callable[[InvestigationState], Awaitable[None]] | None,
) -> InvestigationState:
    result = current
    async for snapshot in graph.astream(graph_input, config, stream_mode="values"):
        result = InvestigationState(**{k: v for k, v in snapshot.items() if k != "__interrupt__"})
        if on_update is not None:
            await on_update(result)
    return result


def to_record(state: InvestigationState) -> InvestigationRecord:
    return InvestigationRecord.model_validate(
        {k: state.get(k) for k in InvestigationRecord.model_fields}
    )


def save_record(state: InvestigationState, directory: Path) -> Path:
    return write_record(to_record(state), directory)


def write_record(record: InvestigationRecord, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{record.investigation_id}.json"
    path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    return path

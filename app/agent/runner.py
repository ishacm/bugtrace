"""Run one investigation through the graph and persist a JSON record of it."""

from collections.abc import Awaitable, Callable
from pathlib import Path

from pydantic import BaseModel

from app.agent.graph import build_graph, initial_state, recursion_limit
from app.agent.model_client import ModelClient
from app.agent.models import (
    AgentEvent,
    Evidence,
    FinalReport,
    Hypothesis,
    InvestigationPlan,
    IssueAnalysis,
    Suspect,
)
from app.agent.state import InvestigationState
from app.agent.tools import RepoToolClient, ToolExecutor
from app.data.models import PublicCase


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


async def run_investigation(
    *,
    case: PublicCase,
    investigation_id: str,
    model: ModelClient,
    repo_client: RepoToolClient,
    max_iterations: int,
    max_tool_calls: int,
    on_update: Callable[[InvestigationState], Awaitable[None]] | None = None,
) -> InvestigationState:
    """Run the graph to completion. `on_update` sees the full state after every node."""
    executor = ToolExecutor(repo_client, max_calls=max_tool_calls)
    graph = build_graph(model, executor)
    state = initial_state(
        investigation_id=investigation_id,
        case=case,
        max_iterations=max_iterations,
        max_tool_calls=max_tool_calls,
    )
    config = {"recursion_limit": recursion_limit(max_iterations)}
    if on_update is None:
        return InvestigationState(**await graph.ainvoke(state, config))
    result = state
    async for snapshot in graph.astream(state, config, stream_mode="values"):
        result = InvestigationState(**snapshot)
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

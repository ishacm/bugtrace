"""Run one investigation through the graph and persist a JSON record of it."""

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
) -> InvestigationState:
    executor = ToolExecutor(repo_client, max_calls=max_tool_calls)
    graph = build_graph(model, executor)
    state = initial_state(
        investigation_id=investigation_id,
        case=case,
        max_iterations=max_iterations,
        max_tool_calls=max_tool_calls,
    )
    result = await graph.ainvoke(state, {"recursion_limit": recursion_limit(max_iterations)})
    return InvestigationState(**result)


def to_record(state: InvestigationState) -> InvestigationRecord:
    return InvestigationRecord.model_validate(
        {k: state.get(k) for k in InvestigationRecord.model_fields}
    )


def save_record(state: InvestigationState, directory: Path) -> Path:
    record = to_record(state)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{record.investigation_id}.json"
    path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    return path

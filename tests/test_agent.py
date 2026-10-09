import ast
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from pydantic import BaseModel

from app.agent.graph import assess_sufficiency, ground_claims, rank_suspects
from app.agent.model_client import ModelError
from app.agent.models import (
    CandidateSelection,
    Claim,
    Confidence,
    Evidence,
    EvidenceType,
    FileReadRequest,
    Hypothesis,
    HypothesisDraft,
    InvestigationPlan,
    IssueAnalysis,
    RootCauseDraft,
    SuspectDraft,
)
from app.agent.runner import run_investigation, save_record, to_record
from app.agent.state import InvestigationState
from app.data.models import PublicCase
from app.data.swebench import public_case_from_row
from app.mcp_servers.client import RepoMCPClient
from app.mcp_servers.repo_tools import RepoTools
from app.workspace.manager import WorkspaceManager, new_investigation_id

ROOT = Path(__file__).resolve().parent.parent
EV_RE = re.compile(r"ev-[0-9a-f]{12}")
FAKE_ID = "ev-000000000000"
GOLD_KEYS = {"patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS", "hints_text"}

Handler = Callable[[str], BaseModel]


class FakeModel:
    """Deterministic stand-in for the model backend, keyed by output schema."""

    def __init__(self, handlers: dict[type[BaseModel], Handler]) -> None:
        self.handlers = handlers
        self.calls: list[tuple[str, str]] = []

    async def generate_structured(self, *, system: str, prompt: str, schema: type[Any]) -> Any:
        self.calls.append((schema.__name__, prompt))
        handler = self.handlers.get(schema)
        if handler is None:
            raise ModelError(f"no stub for {schema.__name__}")
        return handler(prompt)


class LocalRepoClient:
    """In-process async adapter over RepoTools (same typed results as the MCP client)."""

    def __init__(self, tools: RepoTools) -> None:
        self.tools = tools

    async def search_code(self, query, glob=None, max_results=20, regex=False):  # noqa: ANN001
        return self.tools.search_code(query, glob, max_results, regex)

    async def read_file(self, path, start_line=1, end_line=None):  # noqa: ANN001
        return self.tools.read_file(path, start_line, end_line)

    async def find_symbol(self, symbol, max_results=50):  # noqa: ANN001
        return self.tools.find_symbol(symbol, max_results)

    async def git_log(self, path=None, max_count=20):  # noqa: ANN001
        return self.tools.git_log(path, max_count)


def _ids(prompt: str) -> list[str]:
    return list(dict.fromkeys(EV_RE.findall(prompt)))


def _analysis(_: str) -> IssueAnalysis:
    return IssueAnalysis(
        summary="compute_total is off by one",
        expected_behavior="sum of items",
        actual_behavior="sum + 1",
        mentioned_symbols=["compute_total"],
        mentioned_files=[],
        keywords=["compute_total"],
    )


def _plan(_: str) -> InvestigationPlan:
    return InvestigationPlan(
        steps=["find compute_total", "read it"],
        search_queries=["sum(items)"],
        symbols=["compute_total"],
    )


def _selection(_: str) -> CandidateSelection:
    return CandidateSelection(
        reads=[
            FileReadRequest(path="pkg/core.py", start_line=1, end_line=20, reason="definition"),
            FileReadRequest(path="pkg/invented.py", start_line=1, end_line=5, reason="made up"),
        ]
    )


def _hypothesis(prompt: str) -> HypothesisDraft:
    ids = _ids(prompt)
    return HypothesisDraft(
        statement="compute_total adds 1 to the sum",
        evidence_ids=[*ids, FAKE_ID],
        suspects=[
            SuspectDraft(
                path="pkg/core.py", symbol="compute_total", reason="adds 1", evidence_ids=ids
            ),
            SuspectDraft(path="pkg/invented.py", symbol="x", reason="nope", evidence_ids=ids),
        ],
        contradictions=[],
        follow_up_queries=[],
        follow_up_symbols=[],
    )


def _root_cause(prompt: str) -> RootCauseDraft:
    return RootCauseDraft(
        summary="compute_total returns sum(items) + 1.",
        claims=[
            Claim(statement="compute_total adds 1", evidence_ids=_ids(prompt)[:3]),
            Claim(statement="unsupported claim", evidence_ids=[FAKE_ID]),
        ],
        open_questions=[],
    )


def _happy_model() -> FakeModel:
    return FakeModel(
        {
            IssueAnalysis: _analysis,
            InvestigationPlan: _plan,
            CandidateSelection: _selection,
            HypothesisDraft: _hypothesis,
            RootCauseDraft: _root_cause,
        }
    )


CASE_STATEMENT = "`compute_total` returns one more than the sum of its items."


@pytest.fixture(scope="module")
def workspace(
    tmp_path_factory: pytest.TempPathFactory, fixture_repo: tuple[Path, list[str]]
) -> tuple[WorkspaceManager, str, PublicCase]:
    origin, shas = fixture_repo
    manager = WorkspaceManager(tmp_path_factory.mktemp("agent") / "workspaces", git_timeout=60)
    inv = new_investigation_id()
    case = PublicCase(
        instance_id="owner__fixture-1",
        repo="owner/fixture",
        problem_statement=CASE_STATEMENT,
        base_commit=shas[1],
    )
    manager.prepare(inv, case, remote_url=origin.as_uri())
    return manager, inv, case


def _run(
    workspace: tuple[WorkspaceManager, str, PublicCase],
    model: FakeModel,
    *,
    max_iterations: int = 4,
    max_tool_calls: int = 30,
    case: PublicCase | None = None,
) -> InvestigationState:
    manager, inv, default_case = workspace
    client = LocalRepoClient(RepoTools.for_investigation(manager, inv))

    async def go() -> InvestigationState:
        with anyio.fail_after(60):
            return await run_investigation(
                case=case or default_case,
                investigation_id=inv,
                model=model,
                repo_client=client,
                max_iterations=max_iterations,
                max_tool_calls=max_tool_calls,
            )

    return anyio.run(go)


# --- end to end ---------------------------------------------------------------


def test_end_to_end_over_mcp_produces_grounded_report(
    workspace: tuple[WorkspaceManager, str, PublicCase],
) -> None:
    manager, inv, case = workspace
    model = _happy_model()

    async def go() -> InvestigationState:
        with anyio.fail_after(60):
            async with RepoMCPClient.connect(inv, manager.root) as client:
                return await run_investigation(
                    case=case,
                    investigation_id=inv,
                    model=model,
                    repo_client=client,
                    max_iterations=4,
                    max_tool_calls=30,
                )

    state = anyio.run(go)
    report = state["final_report"]
    assert report is not None
    assert report.status == "COMPLETED"
    assert report.confidence in {Confidence.MEDIUM, Confidence.HIGH}

    evidence_ids = {e.evidence_id for e in state["evidence"]}
    assert report.claims and report.cited_evidence_ids
    assert set(report.cited_evidence_ids) <= evidence_ids
    assert all(c.evidence_ids for c in report.claims)
    assert FAKE_ID not in report.cited_evidence_ids
    assert state["root_cause"].dropped_claims == ["unsupported claim"]

    assert [s.path for s in report.suspects] == ["pkg/core.py"]  # invented path rejected
    assert report.suspects[0].rank == 1 and report.suspects[0].symbol == "compute_total"
    assert FAKE_ID not in state["current_hypothesis"].evidence_ids

    read = [e for e in state["evidence"] if e.source == "read_file"]
    assert read and "BUG: off by one" in read[0].excerpt
    assert not any("invented" in f for f in state["files_inspected"])

    types = [e.type for e in state["events"]]
    for expected in ("case_started", "plan_created", "evidence_found", "suspect_found",
                     "hypothesis_created", "root_cause_found"):  # fmt: skip
        assert expected in types
    assert report.tool_calls == state["tool_call_count"] <= 30
    assert report.iterations == 1


# --- bounds -------------------------------------------------------------------


def _never_sufficient(counter: list[int]) -> Handler:
    def handler(_: str) -> HypothesisDraft:
        counter[0] += 1
        return HypothesisDraft(
            statement="unsure",
            evidence_ids=[],
            suspects=[],
            contradictions=[],
            follow_up_queries=[f"lead_{counter[0]}"],
            follow_up_symbols=[f"lead_symbol_{counter[0]}"],
        )

    return handler


def test_loop_is_bounded_by_max_iterations(
    workspace: tuple[WorkspaceManager, str, PublicCase],
) -> None:
    counter = [0]
    model = _happy_model()
    model.handlers[HypothesisDraft] = _never_sufficient(counter)

    state = _run(workspace, model, max_iterations=3, max_tool_calls=100)
    report = state["final_report"]
    assert state["iteration"] == 3 and counter[0] == 3
    assert "max_iterations" in report.limits_reached
    assert report.status == "PARTIAL" and report.confidence == Confidence.LOW


def test_tool_calls_are_bounded(workspace: tuple[WorkspaceManager, str, PublicCase]) -> None:
    model = _happy_model()
    model.handlers[HypothesisDraft] = _never_sufficient([0])

    state = _run(workspace, model, max_iterations=10, max_tool_calls=2)
    report = state["final_report"]
    assert state["tool_call_count"] == report.tool_calls == 2
    assert "max_tool_calls" in report.limits_reached
    assert report.status == "PARTIAL" and report.confidence == Confidence.LOW


def test_stops_when_no_new_leads(workspace: tuple[WorkspaceManager, str, PublicCase]) -> None:
    model = _happy_model()
    model.handlers[HypothesisDraft] = lambda _: HypothesisDraft(
        statement="unsure",
        evidence_ids=[],
        suspects=[],
        contradictions=[],
        follow_up_queries=[],
        follow_up_symbols=[],
    )
    state = _run(workspace, model, max_iterations=4)
    assert "no_new_leads" in state["final_report"].limits_reached
    assert state["iteration"] < 4


def test_insufficient_evidence_triggers_another_search(
    workspace: tuple[WorkspaceManager, str, PublicCase],
) -> None:
    calls = [0]

    def handler(prompt: str) -> HypothesisDraft:
        calls[0] += 1
        if calls[0] == 1:  # first pass: weak, ask to look for the caller
            return HypothesisDraft(
                statement="maybe compute_total",
                evidence_ids=_ids(prompt)[:1],
                suspects=[],
                contradictions=[],
                follow_up_queries=["compute_total([3])"],
                follow_up_symbols=[],
            )
        return _hypothesis(prompt)

    model = _happy_model()
    model.handlers[HypothesisDraft] = handler
    state = _run(workspace, model)
    assert state["iteration"] == 2
    assert "query:compute_total([3])" in state["searches_performed"]
    assert [h.iteration for h in state["hypotheses"]] == [1, 2]
    assert "hypothesis_revised" in [e.type for e in state["events"]]
    assert state["final_report"].status == "COMPLETED"


def test_model_failures_degrade_to_partial_report(
    workspace: tuple[WorkspaceManager, str, PublicCase],
) -> None:
    state = _run(workspace, FakeModel({}), max_iterations=2)
    report = state["final_report"]
    assert report.status == "PARTIAL" and report.confidence == Confidence.LOW
    assert state["errors"]
    # Fallback still searched the backticked symbol from the issue text.
    assert "symbol:compute_total" in state["searches_performed"]


# --- pure logic ---------------------------------------------------------------


def _ev(i: str, type_: EvidenceType, path: str | None = None) -> Evidence:
    return Evidence(evidence_id=i, type=type_, source="t", path=path, excerpt="", summary="")


@pytest.mark.parametrize(
    ("types", "contradictions", "sufficient", "confidence"),
    [
        ([], [], False, Confidence.LOW),
        ([EvidenceType.CODE], [], False, Confidence.LOW),
        ([EvidenceType.ISSUE, EvidenceType.GIT], [], False, Confidence.LOW),
        ([EvidenceType.ISSUE, EvidenceType.CODE], [], True, Confidence.MEDIUM),
        ([EvidenceType.CODE, EvidenceType.TEST], [], True, Confidence.HIGH),
        ([EvidenceType.CODE, EvidenceType.GIT], ["conflict"], True, Confidence.MEDIUM),
    ],
)
def test_evidence_sufficiency_rules(
    types: list[EvidenceType], contradictions: list[str], sufficient: bool, confidence: Confidence
) -> None:
    evidence = [_ev(f"ev-{n:012x}", t) for n, t in enumerate(types)]
    hypothesis = Hypothesis(
        iteration=1,
        statement="h",
        evidence_ids=[e.evidence_id for e in evidence],
        contradictions=contradictions,
    )
    result = assess_sufficiency(hypothesis, evidence)
    assert (result.sufficient, result.confidence) == (sufficient, confidence)


def test_sufficiency_ignores_unknown_ids_and_no_hypothesis() -> None:
    evidence = [_ev("ev-000000000001", EvidenceType.CODE)]
    hypothesis = Hypothesis(
        iteration=1, statement="h", evidence_ids=[*map(lambda e: e.evidence_id, evidence), FAKE_ID]
    )
    assert not assess_sufficiency(hypothesis, evidence).sufficient
    assert not assess_sufficiency(None, evidence).sufficient


def test_ground_claims_drops_unsupported() -> None:
    evidence = [_ev("ev-000000000001", EvidenceType.CODE)]
    grounded, dropped = ground_claims(
        [
            Claim(statement="ok", evidence_ids=["ev-000000000001", FAKE_ID]),
            Claim(statement="bad", evidence_ids=[FAKE_ID]),
            Claim(statement="none", evidence_ids=[]),
        ],
        evidence,
    )
    assert [c.evidence_ids for c in grounded] == [["ev-000000000001"]]
    assert dropped == ["bad", "none"]


def test_rank_suspects_uses_explicit_scoring() -> None:
    evidence = [
        _ev("ev-000000000001", EvidenceType.CODE, "a.py"),
        _ev("ev-000000000002", EvidenceType.TEST, "tests/test_a.py"),
        _ev("ev-000000000003", EvidenceType.CODE, "b.py"),
    ]
    drafts = [
        SuspectDraft(path="b.py", symbol="", reason="r", evidence_ids=["ev-000000000003"]),
        SuspectDraft(
            path="a.py", symbol="f", reason="r", evidence_ids=["ev-000000000001", "ev-000000000002"]
        ),
        SuspectDraft(path="ghost.py", symbol="g", reason="r", evidence_ids=["ev-000000000001"]),
        SuspectDraft(path="a.py", symbol="h", reason="r", evidence_ids=[FAKE_ID]),
    ]
    suspects = rank_suspects(drafts, evidence)
    assert [(s.rank, s.path, s.symbol, s.score) for s in suspects] == [
        (1, "a.py", "f", 3),
        (2, "b.py", None, 2),
    ]


# --- leakage boundary ---------------------------------------------------------


def test_gold_data_absent_from_state_and_record(
    workspace: tuple[WorkspaceManager, str, PublicCase], tmp_path: Path
) -> None:
    _, _, default_case = workspace
    raw_row = {
        **default_case.model_dump(),
        "patch": "diff --git a/pkg/core.py GOLD_PATCH",
        "test_patch": "GOLD_TEST_PATCH",
        "FAIL_TO_PASS": '["GOLD_F2P"]',
        "PASS_TO_PASS": '["GOLD_P2P"]',
        "hints_text": "GOLD_HINT",
    }
    case = public_case_from_row(raw_row)
    model = _happy_model()
    state = _run(workspace, model, case=case)

    assert isinstance(state["case"], PublicCase)
    path = save_record(state, tmp_path)
    text = path.read_text()
    assert "GOLD" not in text
    assert not GOLD_KEYS & set(json.loads(text)["case"])
    assert not GOLD_KEYS & set(state)
    assert all("GOLD" not in prompt for _, prompt in model.calls)
    assert to_record(state).final_report is not None


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_agent_package_has_no_gold_or_raw_dataset_imports() -> None:
    forbidden = ("app.evaluation", "app.data.source", "app.data.swebench", "app.workspace")
    for path in (ROOT / "app" / "agent").rglob("*.py"):
        bad = [m for m in _imports(path) if m.startswith(forbidden)]
        assert bad == [], f"{path.name} imports {bad}"


def test_agent_never_calls_repo_helpers_directly() -> None:
    for path in (ROOT / "app" / "agent").rglob("*.py"):
        assert "app.mcp_servers.repo_tools" not in _imports(path), path.name


def test_root_cause_fallback_is_partial_low_even_with_sufficient_evidence(
    workspace: tuple[WorkspaceManager, str, PublicCase],
) -> None:
    model = _happy_model()
    del model.handlers[RootCauseDraft]  # model fails only at the report step
    state = _run(workspace, model)
    report = state["final_report"]
    assert state["sufficiency"].sufficient
    assert state["root_cause"].generated is False
    assert report.status == "PARTIAL" and report.confidence == Confidence.LOW
    assert report.claims and all(c.evidence_ids for c in report.claims)


class _FakeAPIError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"status {code}")
        self.code = code


def _gemini_with_responses(monkeypatch: pytest.MonkeyPatch, outcomes: list[Any]) -> Any:
    from app.agent.model_client import GeminiModelClient

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = GeminiModelClient(
        api_key="dummy", model_name="test-model", timeout_seconds=5, sleep=fake_sleep
    )
    client._api_error = _FakeAPIError  # type: ignore[attr-defined]

    async def fake_generate(**_: Any) -> Any:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return type("Response", (), {"text": outcome})()

    monkeypatch.setattr(client._client.aio.models, "generate_content", fake_generate)
    client.sleeps = sleeps  # type: ignore[attr-defined]
    return client


def test_gemini_client_retries_transient_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = InvestigationPlan(steps=["a"], search_queries=["b"], symbols=["c"])
    client = _gemini_with_responses(
        monkeypatch, [_FakeAPIError(503), _FakeAPIError(429), plan.model_dump_json()]
    )

    async def go() -> InvestigationPlan:
        return await client.generate_structured(system="s", prompt="p", schema=InvestigationPlan)

    assert anyio.run(go) == plan
    assert client.sleeps == [2.0, 4.0]


def test_gemini_client_gives_up_and_does_not_retry_client_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def go(client: Any) -> None:
        await client.generate_structured(system="s", prompt="p", schema=InvestigationPlan)

    exhausted = _gemini_with_responses(monkeypatch, [_FakeAPIError(503)] * 4)
    with pytest.raises(ModelError, match="after 4 attempt"):
        anyio.run(go, exhausted)
    assert len(exhausted.sleeps) == 3

    bad_request = _gemini_with_responses(monkeypatch, [_FakeAPIError(400)])
    with pytest.raises(ModelError, match="400 after 1 attempt"):
        anyio.run(go, bad_request)
    assert bad_request.sleeps == []

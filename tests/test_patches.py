"""Patch proposal, human approval and privileged application (stub model, no Gemini)."""

import asyncio
import time
import uuid
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agent.models import AgentEvent, PatchDraft
from app.agent.patches import InvalidPatchError, parse_unified_diff, patch_sha256
from app.agent.runner import checkpoint_serde, run_investigation, to_record
from app.config import Settings
from app.data.models import PublicCase
from app.data.store import write_cases
from app.data.swebench import public_case_from_row
from app.main import create_app
from app.mcp_servers.client import PatchMCPClient
from app.mcp_servers.models import ToolErrorCode
from app.mcp_servers.patch_tools import PatchTools
from app.mcp_servers.repo_tools import RepoTools
from app.storage.models import (
    TERMINAL_STATUSES,
    ApprovalDecision,
    DecisionConflictError,
    InvestigationStatus,
)
from app.storage.sqlite import InvestigationStore
from app.workspace.manager import WorkspaceManager, run_git
from tests.conftest import CORE_V1
from tests.test_agent import CASE_STATEMENT, FakeModel, LocalRepoClient, _happy_model, _ids
from tests.test_investigations_api import INSTANCE_ID, FakeBackend, _assert_no_gold, _wait_for

S = InvestigationStatus
ROOT = Path(__file__).resolve().parent.parent
FIX_LINE = "    return sum(items)"
GOOD_DIFF = """--- a/pkg/core.py
+++ b/pkg/core.py
@@ -12,2 +12,2 @@
 def compute_total(items):
-    return sum(items) + 1  # BUG: off by one
+    return sum(items)
"""
# Well-formed, but its context does not match the file: `git apply --check` fails.
STALE_DIFF = GOOD_DIFF.replace(" def compute_total(items):", " def compute_totals(items):")
INVENTED_FILE_DIFF = GOOD_DIFF.replace("pkg/core.py", "pkg/invented.py")


def _patch_model(diff: str = GOOD_DIFF) -> Any:
    def make() -> FakeModel:
        model = _happy_model()
        model.handlers[PatchDraft] = lambda prompt: PatchDraft(
            summary="Drop the extra + 1 in compute_total",
            rationale="compute_total adds 1 to the sum; returning the plain sum fixes it.",
            diff=diff,
            evidence_ids=[*_ids(prompt)[:2], "ev-000000000000"],
        )
        return model

    return make


@pytest.fixture
def penv(tmp_path: Path, fixture_repo: tuple[Path, list[str]]) -> dict[str, Any]:
    origin, shas = fixture_repo
    raw_row = {
        "instance_id": INSTANCE_ID,
        "repo": "owner/fixture",
        "problem_statement": CASE_STATEMENT,
        "base_commit": shas[1],
        "patch": "diff --git a/pkg/core.py GOLD_PATCH",
        "test_patch": "GOLD_TEST_PATCH",
        "FAIL_TO_PASS": '["GOLD_F2P"]',
        "PASS_TO_PASS": '["GOLD_P2P"]',
        "hints_text": "GOLD_HINT",
    }
    case = public_case_from_row(raw_row)
    cases_path = tmp_path / "cases.jsonl"
    write_cases(cases_path, [case])
    settings = Settings(
        _env_file=None,
        cases_path=cases_path,
        database_path=tmp_path / "db" / "bugtrace.db",
        checkpoint_path=tmp_path / "db" / "checkpoints.db",
        workspaces_dir=tmp_path / "workspaces",
        stream_poll_seconds=0.02,
    )
    return {"settings": settings, "origin": origin, "case": case, "tmp": tmp_path}


def _backend(penv: dict[str, Any], diff: str = GOOD_DIFF, **kwargs: Any) -> FakeBackend:
    return FakeBackend(
        penv["tmp"] / "workspaces",
        penv["origin"],
        database_path=penv["settings"].database_path,
        model_factory=_patch_model(diff),
        **kwargs,
    )


@pytest.fixture
def pclient(penv: dict[str, Any]) -> Iterator[TestClient]:
    with TestClient(create_app(settings=penv["settings"], backend=_backend(penv))) as client:
        yield client


def _start(client: TestClient) -> str:
    response = client.post("/api/investigations", json={"instance_id": INSTANCE_ID})
    assert response.status_code == 202, response.text
    return response.json()["investigation_id"]


def _awaiting(client: TestClient) -> tuple[str, dict]:
    inv = _start(client)
    body = _wait_for(client, inv, TERMINAL_STATUSES | {S.AWAITING_APPROVAL})
    assert body["status"] == "AWAITING_APPROVAL", (body["status"], body["patch_error"])
    return inv, body


def _repo(client: TestClient, inv: str) -> Path:
    return client.app.state.investigations.backend.manager.repo_dir(inv)  # type: ignore[attr-defined]


def _git(repo: Path, *args: str) -> str:
    return run_git(repo, *args, timeout=30).stdout


def _decide(client: TestClient, inv: str, decision: str) -> Any:
    return client.post(f"/api/investigations/{inv}/decision", json={"decision": decision})


def _wait_record(
    client: TestClient, inv: str, key: str, value: object, timeout: float = 30
) -> None:
    """Wait for the resumed graph to persist `key == value` on the agent record."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = _store(client).get(inv).record
        if record is not None and getattr(record, key) == value:
            return
        time.sleep(0.05)
    raise AssertionError(f"{key} never became {value!r} for {inv}")


def _store(client: TestClient) -> InvestigationStore:
    return client.app.state.investigations.store  # type: ignore[attr-defined]


# --- diff validation ------------------------------------------------------------


def test_parse_valid_diff_and_normalize_blank_context() -> None:
    parsed = parse_unified_diff(GOOD_DIFF)
    assert parsed.files == ["pkg/core.py"]
    assert (parsed.added, parsed.removed) == (1, 1)
    assert parsed.diff == GOOD_DIFF

    blank_context = (
        "diff --git a/pkg/core.py b/pkg/core.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/pkg/core.py\t2024-01-01\n+++ b/pkg/core.py\n"
        "@@ -11,3 +11,3 @@\n\n def compute_total(items):\n"
        "-    return sum(items) + 1  # BUG: off by one\n+    return sum(items)\n"
    )
    normalized = parse_unified_diff(blank_context)
    assert "\n \n def compute_total" in normalized.diff
    assert "--- a/pkg/core.py\n" in normalized.diff


@pytest.mark.parametrize(
    ("diff", "reason"),
    [
        ("", "empty"),
        ("not a diff at all\n", "unexpected line"),
        ("--- a/pkg/core.py\n+++ b/pkg/core.py\n", "no hunks"),
        (GOOD_DIFF.replace("@@ -12,2 +12,2 @@", "@@ -12,3 +12,2 @@"), "shorter"),
        (GOOD_DIFF.replace("@@ -12,2 +12,2 @@", "@@ -12,1 +12,1 @@"), "longer"),
        (GOOD_DIFF.replace("@@ -12,2 +12,2 @@", "@@ -x +y @@"), "malformed hunk"),
        (GOOD_DIFF.replace("pkg/core.py", "../outside.py"), "escapes"),
        (GOOD_DIFF.replace("a/pkg/core.py", "a//etc/passwd").replace("b/pkg", "b//etc"), "escapes"),
        (GOOD_DIFF.replace("pkg/core.py", ".git/config"), "may not touch"),
        (GOOD_DIFF.replace("pkg/core.py", ".env"), "may not touch"),
        (GOOD_DIFF.replace("--- a/pkg/core.py", "--- /dev/null"), "existing files"),
        (GOOD_DIFF.replace("+++ b/pkg/core.py", "+++ b/pkg/other.py"), "renames"),
        ("new file mode 100644\n" + GOOD_DIFF, "unsupported patch header"),
        ("Binary files a/x and b/x differ\n", "unsupported patch header"),
        (GOOD_DIFF + GOOD_DIFF, "twice"),
        (GOOD_DIFF.replace("+    return sum(items)", "*    return sum(items)"), "invalid hunk"),
        (
            "--- a/pkg/core.py\n+++ b/pkg/core.py\n@@ -12,1 +12,1 @@\n def compute_total(items):\n",
            "no changes",
        ),
    ],
)
def test_invalid_diffs_are_rejected(diff: str, reason: str) -> None:
    with pytest.raises(InvalidPatchError, match=reason):
        parse_unified_diff(diff)


# --- read-only check_patch --------------------------------------------------------


@pytest.fixture
def workspace(
    tmp_path: Path, fixture_repo: tuple[Path, list[str]]
) -> tuple[WorkspaceManager, str, Path]:
    origin, shas = fixture_repo
    manager = WorkspaceManager(tmp_path / "workspaces", git_timeout=60)
    inv = str(uuid.uuid4())
    case = PublicCase(
        instance_id=INSTANCE_ID, repo="o/f", problem_statement="x", base_commit=shas[1]
    )
    manager.prepare(inv, case, remote_url=origin.as_uri())
    return manager, inv, manager.repo_dir(inv)


def test_check_patch_never_mutates(workspace: tuple[WorkspaceManager, str, Path]) -> None:
    manager, inv, repo = workspace
    tools = RepoTools.for_investigation(manager, inv)

    ok = tools.check_patch(GOOD_DIFF)
    assert ok.ok and ok.files == ["pkg/core.py"]

    stale = tools.check_patch(STALE_DIFF)
    assert not stale.ok and stale.error.code == ToolErrorCode.PATCH_REJECTED
    assert "git apply --check failed" in stale.error.message

    missing = tools.check_patch(INVENTED_FILE_DIFF)
    assert not missing.ok and missing.error.code == ToolErrorCode.NOT_FOUND

    traversal = tools.check_patch(GOOD_DIFF.replace("pkg/core.py", "../../etc/passwd"))
    assert not traversal.ok and traversal.error.code == ToolErrorCode.PATCH_REJECTED

    assert _git(repo, "status", "--porcelain") == ""
    assert (repo / "pkg/core.py").read_text() == CORE_V1


def test_check_patch_rejects_symlink_escape(
    workspace: tuple[WorkspaceManager, str, Path], tmp_path: Path
) -> None:
    manager, inv, repo = workspace
    outside = tmp_path / "outside.py"
    outside.write_text(CORE_V1)
    (repo / "link.py").symlink_to(outside)
    result = RepoTools.for_investigation(manager, inv).check_patch(
        GOOD_DIFF.replace("pkg/core.py", "link.py")
    )
    assert not result.ok and result.error.code == ToolErrorCode.PATH_REJECTED
    assert outside.read_text() == CORE_V1


# --- patch generation ---------------------------------------------------------------


def test_completed_root_cause_proposes_patch_and_waits(pclient: TestClient) -> None:
    inv, body = _awaiting(pclient)
    patch = body["proposed_patch"]
    evidence_ids = {e["evidence_id"] for e in body["evidence"]}

    assert body["report"]["status"] == "COMPLETED"
    assert patch["affected_files"] == ["pkg/core.py"]
    assert patch["diff"] == GOOD_DIFF
    assert patch["sha256"] == patch_sha256(GOOD_DIFF)
    assert patch["summary"] and patch["rationale"]
    assert patch["evidence_ids"] and set(patch["evidence_ids"]) <= evidence_ids  # fake id dropped
    assert body["approval"] is None and body["patch_result"] is None
    assert body["allowed_transitions"] == ["CANCELLED", "PATCHING", "UNRESOLVED"]
    types = [e["type"] for e in body["events"]]
    assert types.index("root_cause_found") < types.index("patch_proposed")
    assert types[-1] == "approval_required"

    # Approval is required before mutation: the workspace is untouched while waiting.
    repo = _repo(pclient, inv)
    assert _git(repo, "status", "--porcelain") == ""
    assert (repo / "pkg/core.py").read_text() == CORE_V1


@pytest.mark.parametrize(
    ("diff", "reason"),
    [
        ("this is not a diff", "unexpected line"),
        (STALE_DIFF, "git apply --check failed"),
        (INVENTED_FILE_DIFF, "not found in evidence"),
    ],
)
def test_invalid_patch_is_rejected_safely(penv: dict[str, Any], diff: str, reason: str) -> None:
    backend = _backend(penv, diff)
    with TestClient(create_app(settings=penv["settings"], backend=backend)) as client:
        inv = _start(client)
        body = _wait_for(client, inv, TERMINAL_STATUSES)

        assert body["status"] == "UNRESOLVED"
        assert body["proposed_patch"] is None
        assert reason in body["patch_error"]
        assert body["events"][-1]["title"] == "NO VALID PATCH"
        calls = [p for name, p in backend.models[0].calls if name == "PatchDraft"]
        assert len(calls) == penv["settings"].agent_max_patch_attempts
        assert "Your previous diff was rejected" in calls[-1]
        assert _git(_repo(client, inv), "status", "--porcelain") == ""
        assert _decide(client, inv, "approve").status_code == 409


def test_partial_report_does_not_propose_patch(penv: dict[str, Any]) -> None:
    def no_root_cause() -> FakeModel:
        model = _patch_model()()
        del model.handlers[next(k for k in model.handlers if k.__name__ == "RootCauseDraft")]
        return model

    backend = FakeBackend(
        penv["tmp"] / "workspaces",
        penv["origin"],
        database_path=penv["settings"].database_path,
        model_factory=no_root_cause,
    )
    with TestClient(create_app(settings=penv["settings"], backend=backend)) as client:
        inv = _start(client)
        body = _wait_for(client, inv, TERMINAL_STATUSES)
        assert body["report"]["status"] == "PARTIAL"
        assert body["proposed_patch"] is None and body["patch_error"] is None
        assert all(name != "PatchDraft" for name, _ in backend.models[0].calls)


def test_no_gold_in_patch_prompts_or_responses(pclient: TestClient) -> None:
    inv, body = _awaiting(pclient)
    model = pclient.app.state.investigations.backend.models[0]  # type: ignore[attr-defined]
    patch_prompts = [p for name, p in model.calls if name == "PatchDraft"]
    assert patch_prompts
    assert all("GOLD" not in prompt for _, prompt in model.calls)
    _assert_no_gold(body)
    assert "GOLD" not in pclient.get(f"/api/investigations/{inv}").text
    assert "GOLD" not in pclient.get(f"/investigations/{inv}").text


# --- decisions ------------------------------------------------------------------------


def test_reject_leaves_workspace_unchanged(pclient: TestClient) -> None:
    inv, _ = _awaiting(pclient)
    repo = _repo(pclient, inv)
    head = _git(repo, "rev-parse", "HEAD")

    response = _decide(pclient, inv, "reject")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "UNRESOLVED"
    assert body["approval"]["decision"] == "reject"
    assert body["events"][-1]["type"] == "patch_rejected"

    _wait_record(pclient, inv, "approval_status", "rejected")
    record = _store(pclient).get(inv).record
    assert record is not None and record.patch_result is None
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "rev-parse", "HEAD") == head
    assert (repo / "pkg/core.py").read_text() == CORE_V1
    assert _decide(pclient, inv, "approve").status_code == 409


def test_approve_applies_patch_inside_workspace_only(pclient: TestClient) -> None:
    source_status = _git(ROOT, "status", "--porcelain")
    inv, _ = _awaiting(pclient)
    repo = _repo(pclient, inv)
    head = _git(repo, "rev-parse", "HEAD")

    response = _decide(pclient, inv, "approve")
    assert response.status_code == 200
    assert response.json()["status"] == "PATCHING"
    body = _wait_for(pclient, inv, TERMINAL_STATUSES)

    assert body["status"] == "UNRESOLVED", body["error"]
    result = body["patch_result"]
    assert result["applied"] is True and result["files"] == ["pkg/core.py"]
    assert "-    return sum(items) + 1" in result["diff"] and f"+{FIX_LINE}" in result["diff"]
    types = [e["type"] for e in body["events"]]
    assert types[-2:] == ["patch_approved", "patch_applied"]

    assert FIX_LINE + "\n" in (repo / "pkg/core.py").read_text()
    assert _git(repo, "status", "--porcelain") == " M pkg/core.py\n"
    assert _git(repo, "rev-parse", "HEAD") == head  # never committed
    assert _git(ROOT, "status", "--porcelain") == source_status  # BugTrace tree untouched
    assert _decide(pclient, inv, "approve").status_code == 409


def test_decision_in_wrong_state_returns_409(penv: dict[str, Any]) -> None:
    backend = _backend(penv, prepare_delay=3600)
    with TestClient(create_app(settings=penv["settings"], backend=backend)) as client:
        inv = _start(client)
        assert _decide(client, inv, "approve").status_code == 409  # PREPARING
        client.post(f"/api/investigations/{inv}/cancel")
        assert _decide(client, inv, "reject").status_code == 409  # CANCELLED
        assert _store(client).get(inv).approval is None


def test_decision_request_validation(pclient: TestClient) -> None:
    missing = uuid.uuid4()
    assert _decide(pclient, str(missing), "approve").status_code == 404
    assert _decide(pclient, "not-a-uuid", "approve").status_code == 422
    inv, _ = _awaiting(pclient)
    url = f"/api/investigations/{inv}/decision"
    for payload in ({}, {"decision": "yes"}, {"decision": "approve", "extra": 1}):
        assert pclient.post(url, json=payload).status_code == 422
    assert pclient.get(f"/api/investigations/{inv}").json()["status"] == "AWAITING_APPROVAL"


def test_store_decision_requires_proposed_patch(penv: dict[str, Any]) -> None:
    store = InvestigationStore(penv["settings"].database_path)
    inv = str(uuid.uuid4())
    store.create(inv, penv["case"])
    for target in (S.PREPARING, S.INVESTIGATING, S.AWAITING_APPROVAL):
        store.transition(inv, target)
    event = AgentEvent(type="patch_approved", title="x", message="x")
    with pytest.raises(DecisionConflictError, match="no proposed patch"):
        store.record_decision(inv, ApprovalDecision.APPROVE, event=event)
    assert store.get(inv).status == S.AWAITING_APPROVAL


def test_approval_state_survives_restart(penv: dict[str, Any]) -> None:
    with TestClient(create_app(settings=penv["settings"], backend=_backend(penv))) as client:
        inv, before = _awaiting(client)

    with TestClient(create_app(settings=penv["settings"], backend=_backend(penv))) as client:
        after = client.get(f"/api/investigations/{inv}").json()
        assert after["status"] == "AWAITING_APPROVAL"
        assert after["proposed_patch"] == before["proposed_patch"]
        repo = _repo(client, inv)
        assert _git(repo, "status", "--porcelain") == ""

        # The paused graph resumes from the durable checkpoint in the new process.
        assert _decide(client, inv, "approve").status_code == 200
        body = _wait_for(client, inv, TERMINAL_STATUSES)
        assert body["patch_result"]["applied"] is True
        assert FIX_LINE + "\n" in (repo / "pkg/core.py").read_text()


# --- privileged tool authorization ------------------------------------------------------


def _paused(penv: dict[str, Any]) -> tuple[str, PatchTools, Path, InvestigationStore]:
    with TestClient(create_app(settings=penv["settings"], backend=_backend(penv))) as client:
        inv, _ = _awaiting(client)
        manager = client.app.state.investigations.backend.manager  # type: ignore[attr-defined]
    store = InvestigationStore(penv["settings"].database_path)
    return inv, PatchTools.for_investigation(manager, store, inv), manager.repo_dir(inv), store


def test_apply_patch_before_approval_is_refused(penv: dict[str, Any]) -> None:
    inv, tools, repo, store = _paused(penv)
    result = tools.apply_patch()
    assert not result.ok and not result.applied
    assert result.error.code == ToolErrorCode.NOT_AUTHORIZED
    assert "AWAITING_APPROVAL" in result.error.message
    assert _git(repo, "status", "--porcelain") == ""

    # A PATCHING status without a recorded approval is refused as well.
    store.transition(inv, S.PATCHING)
    refused = tools.apply_patch()
    assert refused.error.code == ToolErrorCode.NOT_AUTHORIZED
    assert "no approval" in refused.error.message
    assert _git(repo, "status", "--porcelain") == ""


def test_apply_patch_refuses_patch_changed_after_approval(penv: dict[str, Any]) -> None:
    inv, tools, repo, store = _paused(penv)
    event = AgentEvent(type="patch_approved", title="x", message="x")
    store.record_decision(inv, ApprovalDecision.APPROVE, event=event)
    record = store.get(inv).record
    assert record is not None and record.proposed_patch is not None
    tampered = record.proposed_patch.model_copy(
        update={"diff": record.proposed_patch.diff.replace("sum(items)\n", "0\n")}
    )
    store.save_record(record.model_copy(update={"proposed_patch": tampered}))

    result = tools.apply_patch()
    assert result.error.code == ToolErrorCode.NOT_AUTHORIZED
    assert "does not match" in result.error.message
    assert _git(repo, "status", "--porcelain") == ""


def test_patch_mcp_server_exposes_only_apply_and_checks_approval(penv: dict[str, Any]) -> None:
    inv, _, repo, _ = _paused(penv)
    settings: Settings = penv["settings"]

    async def scenario() -> tuple[list[str], Any]:
        async with PatchMCPClient.connect(
            inv, settings.workspaces_dir, settings.database_path
        ) as client:
            return await client.list_tool_names(), await client.apply_patch()

    names, result = asyncio.run(scenario())
    assert names == ["apply_patch"]
    assert not result.ok and result.error.code == ToolErrorCode.NOT_AUTHORIZED
    assert _git(repo, "status", "--porcelain") == ""


# --- checkpoint serialization -------------------------------------------------------------


def test_checkpoint_state_round_trips_without_unregistered_types(
    workspace: tuple[WorkspaceManager, str, Path],
) -> None:
    manager, inv, _ = workspace
    case = PublicCase(
        instance_id=INSTANCE_ID,
        repo="o/f",
        problem_statement=CASE_STATEMENT,
        base_commit=_git(manager.repo_dir(inv), "rev-parse", "HEAD").strip(),
    )

    async def scenario() -> Any:
        return await run_investigation(
            case=case,
            investigation_id=inv,
            model=_patch_model()(),
            repo_client=LocalRepoClient(RepoTools.for_investigation(manager, inv)),
            max_iterations=4,
            max_tool_calls=30,
        )

    state = asyncio.run(scenario())
    assert state["approval_status"] == "pending"
    serde = checkpoint_serde()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        restored = serde.loads_typed(serde.dumps_typed(dict(state)))
    assert to_record(restored) == to_record(state)


# --- UI ---------------------------------------------------------------------------------


def test_page_shows_patch_controls_and_outcomes(penv: dict[str, Any]) -> None:
    with TestClient(create_app(settings=penv["settings"], backend=_backend(penv))) as client:
        first, _ = _awaiting(client)
        page = client.get(f"/investigations/{first}").text
        assert "AWAITING APPROVAL" in page
        assert 'data-decision="approve"' in page and 'data-decision="reject"' in page
        assert '<span class="d-del">-    return sum(items) + 1  # BUG: off by one</span>' in page
        assert 'data-terminal="false"' in page  # stream stays open while waiting

        _decide(client, first, "reject")
        page = client.get(f"/investigations/{first}").text
        assert "PATCH REJECTED" in page and "workspace was left unchanged" in page
        assert "data-decision" not in page

        second, _ = _awaiting(client)
        _decide(client, second, "approve")
        _wait_for(client, second, TERMINAL_STATUSES)
        page = client.get(f"/investigations/{second}").text
        assert "PATCH APPLIED" in page and "not committed" in page
        assert "pkg/core.py" in page and "data-decision" not in page


def test_page_shows_why_no_patch_was_proposed(penv: dict[str, Any]) -> None:
    backend = _backend(penv, STALE_DIFF)
    with TestClient(create_app(settings=penv["settings"], backend=backend)) as client:
        inv = _start(client)
        _wait_for(client, inv, TERMINAL_STATUSES)
        page = client.get(f"/investigations/{inv}").text
        assert "No valid patch was proposed" in page
        assert "data-decision" not in page

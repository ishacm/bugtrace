"""The single BugTrace investigation graph.

V1: investigate and explain. V2: after a COMPLETED report, propose a validated
patch, pause at a persistent interrupt for a human decision, and apply the patch
(through the privileged client) only after approval.
"""

import hashlib
import re
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from pydantic import BaseModel

from app.agent import prompts
from app.agent.model_client import ModelClient, ModelError
from app.agent.models import (
    AgentEvent,
    CandidateSelection,
    Claim,
    Confidence,
    Evidence,
    EvidenceType,
    FinalReport,
    Hypothesis,
    HypothesisDraft,
    InvestigationPlan,
    IssueAnalysis,
    PatchDraft,
    PatchResult,
    ProposedPatch,
    RootCause,
    RootCauseDraft,
    Sufficiency,
    Suspect,
)
from app.agent.patches import InvalidPatchError, parse_unified_diff, patch_sha256
from app.agent.state import CandidateHit, InvestigationState
from app.agent.tools import (
    PatchApplier,
    ToolExecutor,
    evidence_from_log,
    evidence_from_read,
    evidence_from_search,
    evidence_from_symbol,
)

MAX_SEARCHES_PER_ITERATION = 6
MAX_READS_PER_ITERATION = 3
MAX_READ_SPAN = 120
MAX_PLAN_SYMBOLS = 5
MAX_PLAN_QUERIES = 5
MAX_CANDIDATES_SHOWN = 15
CODE_GLOB = "**/*.py"
TEST_GLOB = "**/test*.py"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BACKTICK_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]*)")

LIMIT_ITERATIONS = "max_iterations"
LIMIT_TOOL_CALLS = "max_tool_calls"
LIMIT_NO_LEADS = "no_new_leads"


# --- pure helpers (unit-tested directly) -------------------------------------


def assess_sufficiency(hypothesis: Hypothesis | None, evidence: list[Evidence]) -> Sufficiency:
    """Deterministic rules from docs/AGENT.md; never derived from model self-confidence."""
    if hypothesis is None:
        return Sufficiency(sufficient=False, confidence=Confidence.LOW, reasons=["no hypothesis"])
    by_id = {e.evidence_id: e for e in evidence}
    linked = [by_id[i] for i in dict.fromkeys(hypothesis.evidence_ids) if i in by_id]
    types = {e.type for e in linked}
    reasons = []
    if len(linked) < 2:
        reasons.append(f"only {len(linked)} linked evidence record(s); need 2")
    if EvidenceType.CODE not in types:
        reasons.append("no linked code evidence")
    if reasons:
        return Sufficiency(sufficient=False, confidence=Confidence.LOW, reasons=reasons)

    corroborated = bool(types & {EvidenceType.TEST, EvidenceType.GIT, EvidenceType.RUNTIME})
    if corroborated and not hypothesis.contradictions:
        return Sufficiency(
            sufficient=True,
            confidence=Confidence.HIGH,
            reasons=["code evidence corroborated by test/git/runtime evidence"],
        )
    reasons = ["at least 2 linked records including code evidence"]
    if not corroborated:
        reasons.append("no test/git/runtime corroboration")
    if hypothesis.contradictions:
        reasons.append(f"{len(hypothesis.contradictions)} unresolved contradiction(s)")
    return Sufficiency(sufficient=True, confidence=Confidence.MEDIUM, reasons=reasons)


def known_paths(evidence: list[Evidence]) -> set[str]:
    """Repository paths that tools actually returned (evidence paths and match locations)."""
    paths = {e.path for e in evidence if e.path}
    paths |= {
        line.split(":", 1)[0] for e in evidence for line in e.excerpt.splitlines() if ":" in line
    }
    return paths


def rank_suspects(drafts: list[Any], evidence: list[Evidence]) -> list[Suspect]:
    """Score = distinct valid supporting evidence + 1 if any is code evidence on that path."""
    by_id = {e.evidence_id: e for e in evidence}
    paths = known_paths(evidence)
    merged: dict[tuple[str, str | None], Suspect] = {}
    for draft in drafts:
        if draft.path not in paths:
            continue  # never accept a file the tools did not return
        ids = [i for i in dict.fromkeys(draft.evidence_ids) if i in by_id]
        if not ids:
            continue
        symbol = draft.symbol or None
        key = (draft.path, symbol)
        if key in merged:
            ids = list(dict.fromkeys([*merged[key].evidence_ids, *ids]))
        code_bonus = any(
            by_id[i].type == EvidenceType.CODE and by_id[i].path == draft.path for i in ids
        )
        merged[key] = Suspect(
            path=draft.path,
            symbol=symbol,
            rank=0,
            score=len(ids) + int(code_bonus),
            reason=draft.reason,
            evidence_ids=ids,
        )
    ordered = sorted(merged.values(), key=lambda s: (-s.score, s.path, s.symbol or ""))
    return [s.model_copy(update={"rank": n}) for n, s in enumerate(ordered, start=1)]


def ground_claims(claims: list[Claim], evidence: list[Evidence]) -> tuple[list[Claim], list[str]]:
    """Keep only claims citing at least one known evidence ID; strip unknown IDs."""
    known = {e.evidence_id for e in evidence}
    grounded, dropped = [], []
    for claim in claims:
        ids = [i for i in dict.fromkeys(claim.evidence_ids) if i in known]
        if ids:
            grounded.append(Claim(statement=claim.statement, evidence_ids=ids))
        else:
            dropped.append(claim.statement)
    return grounded, dropped


def issue_evidence(problem_statement: str) -> Evidence:
    digest = hashlib.sha256(f"problem_statement\n{problem_statement}".encode()).hexdigest()
    return Evidence(
        evidence_id=f"ev-{digest[:12]}",
        type=EvidenceType.ISSUE,
        source="problem_statement",
        excerpt=problem_statement[:2_000],
        summary="Reported issue text",
    )


def _identifiers(values: list[str]) -> list[str]:
    out = []
    for value in values:
        name = value.strip().strip("`").rstrip("()").split(".")[-1]
        if _IDENTIFIER_RE.match(name) and name not in out:
            out.append(name)
    return out


def _queries(values: list[str]) -> list[str]:
    out = []
    for value in values:
        query = value.strip()
        if 2 < len(query) <= 120 and "\n" not in query and query not in out:
            out.append(query)
    return out


def _format_evidence(evidence: list[Evidence]) -> str:
    blocks = []
    for e in evidence:
        where = f" {e.path}" if e.path else ""
        if e.start_line:
            where += f":{e.start_line}-{e.end_line or e.start_line}"
        excerpt = e.excerpt if len(e.excerpt) <= 1_500 else e.excerpt[:1_500] + "\n…"
        blocks.append(f"[{e.evidence_id}] ({e.type}, {e.source}{where}) {e.summary}\n{excerpt}")
    return "\n\n".join(blocks) or "(none)"


def _covered(path: str, line: int, inspected: list[str]) -> bool:
    for item in inspected:
        p, _, span = item.rpartition(":")
        start, _, end = span.partition("-")
        if p == path and start.isdigit() and end.isdigit() and int(start) <= line <= int(end):
            return True
    return False


def _require[T](value: T | None, name: str) -> T:
    if value is None:
        raise RuntimeError(f"graph invariant violated: {name} missing from state")
    return value


def _event(type_: str, title: str, message: str, **metadata: Any) -> AgentEvent:
    return AgentEvent(type=type_, title=title, message=message, metadata=metadata)  # type: ignore[arg-type]


# --- graph -------------------------------------------------------------------


def _format_report(report: FinalReport) -> str:
    claims = "\n".join(f"- {c.statement} [{', '.join(c.evidence_ids)}]" for c in report.claims)
    return f"{report.summary}\nConfidence: {report.confidence}\nClaims:\n{claims or '(none)'}"


def _format_suspects(suspects: list[Suspect]) -> str:
    lines = [
        f"#{s.rank} {s.path}{'::' + s.symbol if s.symbol else ''} (score {s.score}): {s.reason}"
        f" [{', '.join(s.evidence_ids)}]"
        for s in suspects
    ]
    return "\n".join(lines) or "(none)"


def build_graph(
    model: ModelClient,
    executor: ToolExecutor,
    *,
    patcher: PatchApplier | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    """`patcher` is only supplied when resuming after a human approval."""
    system = prompts.render("system")

    async def ask[T: BaseModel](prompt_name: str, schema: type[T], **values: Any) -> T:
        return await model.generate_structured(
            system=system, prompt=prompts.render(prompt_name, **values), schema=schema
        )

    def analysis_text(state: InvestigationState) -> str:
        analysis = state.get("analysis")
        return analysis.model_dump_json(indent=1) if analysis else "(unavailable)"

    def budget(state: InvestigationState) -> dict[str, Any]:
        limits = list(state["limits_reached"])
        if executor.exhausted and LIMIT_TOOL_CALLS not in limits:
            limits.append(LIMIT_TOOL_CALLS)
        return {"tool_call_count": executor.calls, "limits_reached": limits}

    async def understand_issue(state: InvestigationState) -> dict[str, Any]:
        case = state["case"]
        events = [_event("case_started", "CASE OPENED", f"{case.instance_id} ({case.repo})")]
        errors = list(state["errors"])
        try:
            analysis = await ask(
                "understand_issue",
                IssueAnalysis,
                repo=case.repo,
                problem_statement=case.problem_statement,
            )
        except ModelError as exc:
            errors.append(f"understand_issue: {exc}")
            names = _identifiers(_BACKTICK_RE.findall(case.problem_statement))
            analysis = IssueAnalysis(
                summary=case.problem_statement[:300],
                expected_behavior="",
                actual_behavior="",
                mentioned_symbols=names,
                mentioned_files=[],
                keywords=[],
            )
        return {
            "analysis": analysis,
            "evidence": [issue_evidence(case.problem_statement)],
            "events": [*state["events"], *events],
            "errors": errors,
        }

    async def create_plan(state: InvestigationState) -> dict[str, Any]:
        analysis = _require(state["analysis"], "analysis")
        errors = list(state["errors"])
        try:
            plan = await ask(
                "create_plan",
                InvestigationPlan,
                repo=state["case"].repo,
                analysis=analysis_text(state),
                max_symbols=MAX_PLAN_SYMBOLS,
                max_queries=MAX_PLAN_QUERIES,
            )
        except ModelError as exc:
            errors.append(f"create_plan: {exc}")
            plan = InvestigationPlan(
                steps=["Locate symbols mentioned in the issue", "Read their definitions"],
                search_queries=analysis.keywords,
                symbols=analysis.mentioned_symbols,
            )
        symbols = _identifiers([*analysis.mentioned_symbols, *plan.symbols])[: MAX_PLAN_SYMBOLS * 2]
        queries = _queries(plan.search_queries)[:MAX_PLAN_QUERIES]
        event = _event(
            "plan_created",
            "INVESTIGATION PLAN",
            "; ".join(plan.steps),
            symbols=symbols,
            queries=queries,
        )
        return {
            "plan": plan,
            "pending_symbols": symbols,
            "pending_queries": queries,
            "events": [*state["events"], event],
            "errors": errors,
        }

    async def search_repository(state: InvestigationState) -> dict[str, Any]:
        performed = list(state["searches_performed"])
        evidence = list(state["evidence"])
        known_ids = {e.evidence_id for e in evidence}
        events = list(state["events"])
        hits: dict[tuple[str, int], CandidateHit] = {
            (c.path, c.line): c for c in state["candidates"]
        }

        def add_hit(path: str, line: int, snippet: str, points: int) -> None:
            key = (path, line)
            prior = hits.get(key)
            score = (prior.score if prior else 0) + points
            hits[key] = CandidateHit(path=path, line=line, snippet=snippet.strip(), score=score)

        def add_evidence(item: Evidence) -> None:
            if item.evidence_id not in known_ids:
                known_ids.add(item.evidence_id)
                evidence.append(item)
                events.append(
                    _event("evidence_found", "NEW EVIDENCE", item.summary, id=item.evidence_id)
                )

        work = [("symbol", s) for s in state["pending_symbols"]]
        work += [("query", q) for q in state["pending_queries"]]
        work = [(k, v) for k, v in work if f"{k}:{v}" not in performed]
        work = work[:MAX_SEARCHES_PER_ITERATION]
        if work:
            events.append(
                _event(
                    "search_started",
                    "SEARCHING",
                    ", ".join(v for _, v in work),
                    iteration=state["iteration"],
                )
            )
        for kind, value in work:
            if kind == "symbol":
                result = await executor.find_symbol(value)
            else:
                result = await executor.search_code(value, CODE_GLOB, 20)
            if result is None:
                break
            performed.append(f"{kind}:{value}")
            if not result.ok or not result.matches:
                continue
            if kind == "symbol":
                add_evidence(evidence_from_symbol(result))
                for m in result.matches:
                    add_hit(m.path, m.line, m.snippet, 3)
            else:
                add_evidence(evidence_from_search(result, glob=CODE_GLOB))
                for m in result.matches:
                    add_hit(m.path, m.line, m.snippet, 1)

        remaining_symbols = [s for s in state["pending_symbols"] if f"symbol:{s}" not in performed]
        remaining_queries = [q for q in state["pending_queries"] if f"query:{q}" not in performed]
        return {
            "evidence": evidence,
            "candidates": sorted(hits.values(), key=lambda c: (-c.score, c.path, c.line)),
            "searches_performed": performed,
            "pending_symbols": remaining_symbols,
            "pending_queries": remaining_queries,
            "events": events,
            **budget(state),
        }

    def open_candidates(state: InvestigationState) -> list[CandidateHit]:
        inspected = state["files_inspected"]
        return [c for c in state["candidates"] if not _covered(c.path, c.line, inspected)]

    async def inspect_candidates(state: InvestigationState) -> dict[str, Any]:
        candidates = open_candidates(state)[:MAX_CANDIDATES_SHOWN]
        if not candidates or executor.exhausted:
            return budget(state)
        errors = list(state["errors"])
        allowed = {c.path for c in candidates}
        try:
            selection = await ask(
                "select_candidates",
                CandidateSelection,
                analysis=analysis_text(state),
                candidates="\n".join(
                    f"{c.path}:{c.line}: {c.snippet[:160]} (score {c.score})" for c in candidates
                ),
                inspected="\n".join(state["files_inspected"]) or "(none)",
                max_reads=MAX_READS_PER_ITERATION,
                max_span=MAX_READ_SPAN,
            )
            reads = [
                (r.path, r.start_line, r.end_line, r.reason)
                for r in selection.reads
                if r.path in allowed
            ]
        except ModelError as exc:
            errors.append(f"inspect_candidates: {exc}")
            reads = []
        if not reads:  # deterministic fallback: top-scored hits
            reads = [
                (c.path, max(1, c.line - 20), c.line + 40, f"top search hit (score {c.score})")
                for c in candidates[:2]
            ]

        evidence = list(state["evidence"])
        known_ids = {e.evidence_id for e in evidence}
        inspected = list(state["files_inspected"])
        events = list(state["events"])
        for path, start, end, reason in reads[:MAX_READS_PER_ITERATION]:
            start = max(1, start)
            end = min(max(start, end), start + MAX_READ_SPAN - 1)
            result = await executor.read_file(path, start, end)
            if result is None:
                break
            if not result.ok:
                errors.append(f"read_file {path}: {result.error.message if result.error else ''}")
                continue
            inspected.append(f"{result.path}:{result.start_line}-{result.end_line}")
            item = evidence_from_read(result, reason)
            if item.evidence_id not in known_ids:
                known_ids.add(item.evidence_id)
                evidence.append(item)
                events.append(
                    _event("evidence_found", "NEW EVIDENCE", item.summary, id=item.evidence_id)
                )
        return {
            "evidence": evidence,
            "files_inspected": inspected,
            "events": events,
            "errors": errors,
            **budget(state),
        }

    async def inspect_context(state: InvestigationState) -> dict[str, Any]:
        """Tests and git history for the leading candidate, once per path/symbol."""
        top = state["suspects"][0] if state["suspects"] else None
        path = top.path if top else (state["candidates"][0].path if state["candidates"] else None)
        symbol = top.symbol if top and top.symbol else None
        if symbol is None and state["searches_performed"]:
            symbols = [
                s.split(":", 1)[1] for s in state["searches_performed"] if s[:7] == "symbol:"
            ]
            symbol = symbols[0] if symbols else None

        performed = list(state["searches_performed"])
        evidence = list(state["evidence"])
        known_ids = {e.evidence_id for e in evidence}
        events = list(state["events"])

        def add(item: Evidence) -> None:
            if item.evidence_id not in known_ids:
                known_ids.add(item.evidence_id)
                evidence.append(item)
                events.append(
                    _event("evidence_found", "NEW EVIDENCE", item.summary, id=item.evidence_id)
                )

        if symbol and f"tests:{symbol}" not in performed:
            result = await executor.search_code(symbol, TEST_GLOB, 10)
            if result is not None:
                performed.append(f"tests:{symbol}")
                if result.ok and result.matches:
                    add(evidence_from_search(result, glob=TEST_GLOB, kind=EvidenceType.TEST))
        if path and f"git_log:{path}" not in performed:
            result = await executor.git_log(path)
            if result is not None:
                performed.append(f"git_log:{path}")
                if result.ok and result.commits:
                    add(evidence_from_log(result))
        return {
            "evidence": evidence,
            "searches_performed": performed,
            "events": events,
            **budget(state),
        }

    async def formulate_hypothesis(state: InvestigationState) -> dict[str, Any]:
        evidence = state["evidence"]
        previous = state.get("current_hypothesis")
        errors = list(state["errors"])
        events = list(state["events"])
        try:
            draft = await ask(
                "formulate_hypothesis",
                HypothesisDraft,
                analysis=analysis_text(state),
                previous=previous.statement if previous else "(none)",
                evidence=_format_evidence(evidence),
            )
        except ModelError as exc:
            errors.append(f"formulate_hypothesis: {exc}")
            return {"errors": errors}

        known = {e.evidence_id for e in evidence}
        hypothesis = Hypothesis(
            iteration=state["iteration"],
            statement=draft.statement,
            evidence_ids=[i for i in dict.fromkeys(draft.evidence_ids) if i in known],
            contradictions=draft.contradictions,
        )
        suspects = rank_suspects(draft.suspects, evidence)
        old = {(s.path, s.symbol) for s in state["suspects"]}
        new = {(s.path, s.symbol) for s in suspects}
        for s in suspects:
            if (s.path, s.symbol) not in old:
                label = f"{s.path}::{s.symbol}" if s.symbol else s.path
                events.append(
                    _event("suspect_found", "PRIMARY SUSPECT" if s.rank == 1 else "SUSPECT",
                           f"#{s.rank} {label}", score=s.score, evidence_ids=s.evidence_ids)
                )  # fmt: skip
        for path, symbol in old - new:
            events.append(_event("suspect_removed", "SUSPECT CLEARED", f"{path}::{symbol or ''}"))
        events.append(
            _event(
                "hypothesis_revised" if previous else "hypothesis_created",
                "HYPOTHESIS",
                hypothesis.statement,
                evidence_ids=hypothesis.evidence_ids,
            )
        )
        searched = set(state["searches_performed"])
        follow_symbols = [
            s for s in _identifiers(draft.follow_up_symbols) if f"symbol:{s}" not in searched
        ]
        follow_queries = [
            q for q in _queries(draft.follow_up_queries) if f"query:{q}" not in searched
        ]
        return {
            "current_hypothesis": hypothesis,
            "hypotheses": [*state["hypotheses"], hypothesis],
            "suspects": suspects,
            "pending_symbols": list(dict.fromkeys([*state["pending_symbols"], *follow_symbols])),
            "pending_queries": list(dict.fromkeys([*state["pending_queries"], *follow_queries])),
            "events": events,
            "errors": errors,
        }

    def verify_evidence(state: InvestigationState) -> dict[str, Any]:
        sufficiency = assess_sufficiency(state.get("current_hypothesis"), state["evidence"])
        limits = list(state["limits_reached"])
        if sufficiency.sufficient:
            return {"sufficiency": sufficiency, "next_step": "produce_root_cause"}

        has_leads = bool(
            state["pending_symbols"] or state["pending_queries"] or open_candidates(state)
        )
        if state["iteration"] >= state["max_iterations"]:
            limits.append(LIMIT_ITERATIONS)
        elif executor.exhausted:
            limits.append(LIMIT_TOOL_CALLS)
        elif not has_leads:
            limits.append(LIMIT_NO_LEADS)
        else:
            return {
                "sufficiency": sufficiency,
                "next_step": "search_repository",
                "iteration": state["iteration"] + 1,
            }
        return {
            "sufficiency": sufficiency,
            "next_step": "produce_root_cause",
            "limits_reached": list(dict.fromkeys(limits)),
        }

    async def produce_root_cause(state: InvestigationState) -> dict[str, Any]:
        sufficiency = _require(state["sufficiency"], "sufficiency")
        hypothesis = state.get("current_hypothesis")
        evidence = state["evidence"]
        errors = list(state["errors"])
        generated = True
        try:
            draft = await ask(
                "root_cause",
                RootCauseDraft,
                analysis=analysis_text(state),
                hypothesis=hypothesis.model_dump_json() if hypothesis else "(none)",
                sufficiency=sufficiency.model_dump_json(),
                evidence=_format_evidence(evidence),
            )
        except ModelError as exc:
            errors.append(f"produce_root_cause: {exc}")
            generated = False
            draft = RootCauseDraft(
                summary=(hypothesis.statement if hypothesis else "No hypothesis formed.")
                + " (Report generation failed; partial result.)",
                claims=[Claim(statement=hypothesis.statement, evidence_ids=hypothesis.evidence_ids)]
                if hypothesis
                else [],
                open_questions=[],
            )

        claims, dropped = ground_claims(draft.claims, evidence)
        trusted = sufficiency.sufficient and claims and generated
        confidence = sufficiency.confidence if trusted else Confidence.LOW
        root_cause = RootCause(
            summary=draft.summary,
            claims=claims,
            open_questions=draft.open_questions,
            confidence=confidence,
            dropped_claims=dropped,
            generated=generated,
        )
        event = _event(
            "root_cause_found",
            "ROOT CAUSE",
            root_cause.summary,
            confidence=confidence,
            dropped_claims=len(dropped),
        )
        return {"root_cause": root_cause, "events": [*state["events"], event], "errors": errors}

    def finalize(state: InvestigationState) -> dict[str, Any]:
        root_cause = _require(state["root_cause"], "root_cause")
        sufficiency = _require(state["sufficiency"], "sufficiency")
        cited = list(dict.fromkeys(i for c in root_cause.claims for i in c.evidence_ids))
        report = FinalReport(
            investigation_id=state["investigation_id"],
            instance_id=state["case"].instance_id,
            status="COMPLETED"
            if sufficiency.sufficient and root_cause.claims and root_cause.generated
            else "PARTIAL",
            confidence=root_cause.confidence,
            summary=root_cause.summary,
            claims=root_cause.claims,
            suspects=state["suspects"],
            cited_evidence_ids=cited,
            open_questions=[
                *root_cause.open_questions,
                *([] if sufficiency.sufficient else sufficiency.reasons),
            ],
            limits_reached=state["limits_reached"],
            tool_calls=executor.calls,
            iterations=state["iteration"],
        )
        return {"final_report": report, "tool_call_count": executor.calls}

    # --- V2 patch stage ---------------------------------------------------------

    async def validated_patch(
        draft: PatchDraft, attempt: int, evidence: list[Evidence]
    ) -> ProposedPatch:
        parsed = parse_unified_diff(draft.diff)
        grounded = known_paths(evidence)
        ungrounded = [f for f in parsed.files if f not in grounded]
        if ungrounded:
            raise InvalidPatchError(f"patch modifies files not found in evidence: {ungrounded}")
        known_ids = {e.evidence_id for e in evidence}
        ids = [i for i in dict.fromkeys(draft.evidence_ids) if i in known_ids]
        if not ids:
            raise InvalidPatchError("patch cites no known evidence IDs")
        check = await executor.check_patch(parsed.diff)
        if not check.ok:
            raise InvalidPatchError(check.error.message if check.error else "patch check failed")
        sha = patch_sha256(parsed.diff)
        return ProposedPatch(
            patch_id=f"patch-{sha[:12]}",
            sha256=sha,
            attempt=attempt,
            summary=draft.summary,
            rationale=draft.rationale,
            diff=parsed.diff,
            affected_files=parsed.files,
            evidence_ids=ids,
            added_lines=parsed.added,
            removed_lines=parsed.removed,
        )

    async def propose_patch(state: InvestigationState) -> dict[str, Any]:
        report = _require(state["final_report"], "final_report")
        evidence = state["evidence"]
        suspect_paths = {s.path for s in state["suspects"]}
        code = [e for e in evidence if e.type == EvidenceType.CODE]
        focused = [e for e in code if e.path in suspect_paths] or code
        errors, events = list(state["errors"]), list(state["events"])
        feedback, problem = "", "no attempts made"
        for attempt in range(1, state["max_patch_attempts"] + 1):
            try:
                draft = await ask(
                    "propose_patch",
                    PatchDraft,
                    root_cause=_format_report(report),
                    suspects=_format_suspects(state["suspects"]),
                    evidence=_format_evidence(focused),
                    feedback=feedback,
                )
            except ModelError as exc:
                errors.append(f"propose_patch: {exc}")
                problem = f"model error: {exc}"
                break
            try:
                patch = await validated_patch(draft, attempt, evidence)
            except InvalidPatchError as exc:
                problem = str(exc)
                feedback = f"\nYour previous diff was rejected: {problem}\nCorrect it.\n"
                continue
            events.append(
                _event(
                    "patch_proposed",
                    "PATCH PROPOSED",
                    patch.summary,
                    patch_id=patch.patch_id,
                    files=patch.affected_files,
                )
            )
            return {
                "proposed_patch": patch,
                "patch_error": None,
                "approval_status": "pending",
                "events": events,
                "errors": errors,
            }
        events.append(_event("error", "NO VALID PATCH", problem))
        return {"proposed_patch": None, "patch_error": problem, "events": events, "errors": errors}

    def pause_for_approval(state: InvestigationState) -> dict[str, Any]:
        patch = _require(state["proposed_patch"], "proposed_patch")
        # Persistent pause: the run stops here until resumed with a human decision.
        decision = interrupt({"patch_id": patch.patch_id, "sha256": patch.sha256})
        approved = (
            isinstance(decision, dict)
            and decision.get("decision") == "approve"
            and decision.get("sha256") == patch.sha256
        )
        return {"approval_status": "approved" if approved else "rejected"}

    async def apply_patch(state: InvestigationState) -> dict[str, Any]:
        if state.get("approval_status") != "approved" or patcher is None:
            raise RuntimeError("apply_patch reached without approval or patch client")
        result = await patcher.apply_patch()
        events = list(state["events"])
        if result.ok and result.applied:
            outcome = PatchResult(
                applied=True,
                message="Patch applied in the investigation workspace (not committed).",
                files=result.files,
                stat=result.stat,
                diff=result.diff,
            )
            events.append(_event("patch_applied", "PATCH APPLIED", ", ".join(result.files)))
        else:
            message = result.error.message if result.error else "patch was not applied"
            outcome = PatchResult(applied=False, message=message)
            events.append(_event("error", "PATCH NOT APPLIED", message))
        return {"patch_result": outcome, "events": events}

    graph = StateGraph(InvestigationState)
    graph.add_node("understand_issue", understand_issue)
    graph.add_node("create_plan", create_plan)
    graph.add_node("search_repository", search_repository)
    graph.add_node("inspect_candidates", inspect_candidates)
    graph.add_node("inspect_context", inspect_context)
    graph.add_node("formulate_hypothesis", formulate_hypothesis)
    graph.add_node("verify_evidence", verify_evidence)
    graph.add_node("produce_root_cause", produce_root_cause)
    graph.add_node("finalize", finalize)
    graph.add_node("propose_patch", propose_patch)
    graph.add_node("pause_for_approval", pause_for_approval)
    graph.add_node("apply_patch", apply_patch)

    graph.add_edge(START, "understand_issue")
    graph.add_edge("understand_issue", "create_plan")
    graph.add_edge("create_plan", "search_repository")
    graph.add_edge("search_repository", "inspect_candidates")
    graph.add_edge("inspect_candidates", "inspect_context")
    graph.add_edge("inspect_context", "formulate_hypothesis")
    graph.add_edge("formulate_hypothesis", "verify_evidence")
    graph.add_conditional_edges(
        "verify_evidence",
        lambda state: state["next_step"],
        {"search_repository": "search_repository", "produce_root_cause": "produce_root_cause"},
    )
    graph.add_edge("produce_root_cause", "finalize")
    # Patches are proposed only for a COMPLETED (grounded, sufficient) root cause.
    graph.add_conditional_edges(
        "finalize",
        lambda state: "propose_patch" if state["final_report"].status == "COMPLETED" else END,
        {"propose_patch": "propose_patch", END: END},
    )
    graph.add_conditional_edges(
        "propose_patch",
        lambda state: "pause_for_approval" if state.get("proposed_patch") else END,
        {"pause_for_approval": "pause_for_approval", END: END},
    )
    graph.add_conditional_edges(
        "pause_for_approval",
        lambda state: "apply_patch" if state["approval_status"] == "approved" else END,
        {"apply_patch": "apply_patch", END: END},
    )
    graph.add_edge("apply_patch", END)
    return graph.compile(checkpointer=checkpointer)


def initial_state(
    *,
    investigation_id: str,
    case: Any,
    max_iterations: int,
    max_tool_calls: int,
    max_patch_attempts: int = 2,
) -> InvestigationState:
    return InvestigationState(
        investigation_id=investigation_id,
        case=case,
        analysis=None,
        plan=None,
        evidence=[],
        candidates=[],
        suspects=[],
        hypotheses=[],
        current_hypothesis=None,
        sufficiency=None,
        root_cause=None,
        final_report=None,
        pending_symbols=[],
        pending_queries=[],
        searches_performed=[],
        files_inspected=[],
        tool_call_count=0,
        iteration=1,
        max_iterations=max_iterations,
        max_tool_calls=max_tool_calls,
        next_step="search_repository",
        limits_reached=[],
        errors=[],
        events=[],
        max_patch_attempts=max_patch_attempts,
        proposed_patch=None,
        patch_error=None,
        approval_status=None,
        patch_result=None,
        test_result=None,
    )


# Each loop pass visits 5 nodes; this backstop sits well above any bounded run
# (including the patch stage after finalize).
def recursion_limit(max_iterations: int) -> int:
    return 14 + 6 * max_iterations

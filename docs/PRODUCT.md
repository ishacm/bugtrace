# Product Scope

## Product
**BugTrace — An AI detective for real software incidents.**

BugTrace takes a real SWE-bench issue and the corresponding repository state before the human fix. It investigates the codebase, gathers evidence, ranks likely responsible files/functions, explains the root cause, proposes a patch, pauses for human approval, and then evaluates the patch.

## Primary user story
As an engineer, I can select a real bug case and watch BugTrace investigate it so that I can understand what the agent inspected, what evidence supports its conclusion, what code it wants to change, and whether the proposed fix actually works.

## Product flow
1. Select case.
2. Prepare repository at `base_commit`.
3. Open investigation.
4. Build investigation plan.
5. Search and inspect repository.
6. Collect evidence.
7. Rank suspects.
8. Form and revise hypothesis.
9. Produce root-cause report.
10. V2: propose patch.
11. V2: pause for explicit approval.
12. V2: apply patch and evaluate.
13. Show `CASE CLOSED` (harness resolved) or the unresolved outcome.

As built: automatic reopening after a failed evaluation is not implemented. A failed
evaluation ends the case UNRESOLVED (see `docs/AGENT.md`).

## Versions
### V1 — Investigator
Must include SWE-bench case selection, isolated repository workspace, MCP read tools, explicit LangGraph investigation, evidence, suspects, root cause, FastAPI, persistence, and a simple live UI.

### V2 — Resolver
Add patch generation, human approval, pause/resume, patch application, official SWE-bench evaluation, replay, evaluation metrics, and CI/regression checks.

### V3 — Production-style
Add model backend abstraction, Vertex AI, Cloud Run, persistent production storage, and optionally one of: pgvector incident memory, n8n trigger, or multimodal SWE-bench support.

## Non-goals
- General-purpose coding assistant
- IDE extension
- Full SWE-bench leaderboard competitor
- Multi-agent swarm
- Autonomous remote writes
- Large SPA frontend
- Kubernetes/microservices showcase

## Portfolio success criterion
Five to ten genuine cases should demonstrate a real end-to-end investigation. A stable V2 is more important than many V3 features.

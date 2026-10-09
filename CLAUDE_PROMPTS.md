# BugTrace — Exact Claude Code Prompt Sequence

Use Claude Code with Opus 5.5 selected. The goal is to keep context small and implementation reliable.

## Before starting
Create an empty project folder on your Mac, copy this entire pack into it, open the folder in VS Code, then launch Claude Code from that folder.

Recommended shell flow:

```bash
mkdir -p ~/Projects/bugtrace
cd ~/Projects/bugtrace
# copy the contents of this pack here
code .
claude
```

Inside Claude Code, select Opus 5.5 using `/model` and verify the selected model.

## Important usage rule
Do not paste the full PRD into Claude. The repository files are the memory. Each task prompt below tells Claude exactly what to read.

---

## TASK 001 — Bootstrap
Paste:

> Read `CLAUDE.md`, `PROGRESS.md`, and `tasks/001-bootstrap.md`. Read only the reference docs explicitly named by that task. Implement TASK 001 only. Keep chat output concise, run all required checks, update `PROGRESS.md`, and stop when the task passes. Do not start TASK 002.

After it succeeds, inspect the summary. Then manually commit if desired:

```bash
git add .
git commit -m "feat: bootstrap BugTrace application"
```

---

## TASK 002 — SWE-bench safe data adapter
Paste:

> Read `CLAUDE.md`, `PROGRESS.md`, and `tasks/002-swebench.md`. Read only the docs explicitly referenced by the task. Implement TASK 002 only. Preserve the strict gold-data leakage boundary. Run the required tests and linting, update `PROGRESS.md`, and stop.

Suggested commit:

```bash
git add .
git commit -m "feat: add safe SWE-bench data adapter"
```

---

## TASK 003 — Repository workspace manager
Paste:

> Read `CLAUDE.md`, `PROGRESS.md`, and `tasks/003-workspace.md`. Implement TASK 003 only. Pay special attention to workspace isolation, Git checkout reproducibility, subprocess timeouts, and path safety. Run tests and linting, update `PROGRESS.md`, and stop.

Suggested commit:

```bash
git add .
git commit -m "feat: add isolated repository workspaces"
```

### Clear context here
Use `/clear` before TASK 004. The filesystem, Git history, and `PROGRESS.md` are the project memory.

---

## TASK 004 — MCP repository tools
Paste:

> Read `CLAUDE.md`, `PROGRESS.md`, and `tasks/004-mcp.md`. Read only the referenced docs. Implement the repository MCP server and its tests. Do not add patch/write tools yet. Run tests and linting, update `PROGRESS.md`, and stop.

Suggested commit:

```bash
git add .
git commit -m "feat: expose repository tools through MCP"
```

---

## TASK 005 — LangGraph investigation agent
Paste:

> Read `CLAUDE.md`, `PROGRESS.md`, and `tasks/005-agent.md`. Read only the referenced docs and source files needed for the task. Implement one explicit LangGraph investigation graph; do not replace it with a generic ReAct helper. Root-cause claims must cite evidence IDs. Run tests and linting, update `PROGRESS.md`, and stop.

Suggested commit:

```bash
git add .
git commit -m "feat: implement evidence-driven investigation graph"
```

---

## TASK 006 — API + persistence
Paste:

> Read `CLAUDE.md`, `PROGRESS.md`, and `tasks/006-api-persistence.md`. Implement TASK 006 only. Ensure investigation state and events persist in SQLite and API state transitions are validated. Run tests and linting, update `PROGRESS.md`, and stop.

Suggested commit:

```bash
git add .
git commit -m "feat: add investigation API and persistence"
```

### Clear context here
Use `/clear` before TASK 007.

---

## TASK 007 — Detective UI + streaming
Paste:

> Read `CLAUDE.md`, `PROGRESS.md`, and `tasks/007-ui-streaming.md`. Implement TASK 007 only. Use Jinja2, HTML, CSS and vanilla JavaScript; do not add React. The UI must render real backend state, not fake demo data. Run tests/linting as applicable, update `PROGRESS.md`, and stop.

Suggested commit:

```bash
git add .
git commit -m "feat: add live BugTrace investigation UI"
```

---

## TASK 008 — Patch + human approval
Paste:

> Read `CLAUDE.md`, `PROGRESS.md`, and `tasks/008-patch-approval.md`. Implement TASK 008 only. Repository mutations must be impossible before explicit approval. Use LangGraph pause/resume persistence. Add security tests proving the write boundary. Run tests and linting, update `PROGRESS.md`, and stop.

Suggested commit:

```bash
git add .
git commit -m "feat: add approval-gated patch workflow"
```

---

## TASK 009 — SWE-bench evaluation
Paste:

> Read `CLAUDE.md`, `PROGRESS.md`, and `tasks/009-evaluation.md`. Implement TASK 009 only. Integrate the official SWE-bench evaluation path where practical; do not leak gold fields into the agent. Start with a very small number of cases. Run tests/linting, update `PROGRESS.md`, and stop.

Suggested commit:

```bash
git add .
git commit -m "feat: integrate SWE-bench patch evaluation"
```

### Clear context here
Use `/clear` before TASK 010.

---

## TASK 010 — Regression evals + CI
Paste:

> Read `CLAUDE.md`, `PROGRESS.md`, and `tasks/010-ci-regression.md`. Implement TASK 010 only. Keep normal CI cheap; full agent evaluation must be manual or scheduled. Never fabricate result numbers. Run local checks, update `PROGRESS.md`, and stop.

Suggested commit:

```bash
git add .
git commit -m "ci: add BugTrace regression evaluation workflows"
```

---

## TASK 011 — Vertex AI + Cloud Run
Paste:

> Read `CLAUDE.md`, `PROGRESS.md`, and `tasks/011-cloud.md`. Implement TASK 011 only. Preserve a single model-client abstraction so local Gemini API and Vertex AI share the same agent code. Deploy only the web/API service to Cloud Run; do not attempt Docker-in-Docker there. Run tests and linting, update `PROGRESS.md`, and stop.

Suggested commit:

```bash
git add .
git commit -m "feat: add Vertex AI backend and Cloud Run deployment"
```

---

## TASK 012 — Polish
Paste:

> Read `CLAUDE.md`, `PROGRESS.md`, and `tasks/012-polish.md`. Implement TASK 012 only. Prioritize reliability, README clarity, demo quality, and honest measured results. Add at most one optional V3 extension if the core system is stable. Run the final checks, update `PROGRESS.md`, and stop.

Suggested commit:

```bash
git add .
git commit -m "docs: polish BugTrace portfolio release"
```

---

# Final review prompt
After TASK 012, use `/clear`, then paste:

> Perform a final release review of BugTrace. Read `CLAUDE.md`, `PROGRESS.md`, the relevant architecture/evaluation docs, and inspect the current repository. Do not add new features. Look only for correctness issues, security boundary violations, gold-data leakage, broken setup instructions, dead code, failing tests, and claims in README that are not backed by implemented functionality or measured results. Fix only clear defects, run the final test/lint suite, and give me a concise release checklist.

# When Claude gets stuck
Do not tell it to rebuild everything. Use:

> Diagnose only the current failure. Inspect the smallest relevant set of files and the failing test/output. Explain the root cause in 3 bullets maximum, implement the minimal fix, rerun the failing test first, then the relevant suite. Do not refactor unrelated code.

# When token/context use gets high
Use `/clear`, then:

> Read `CLAUDE.md`, `PROGRESS.md`, and the current task only. Reconstruct project state from the filesystem and Git, not conversation history. Continue only the current task.

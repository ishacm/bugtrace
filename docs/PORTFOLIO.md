# Portfolio Summary

## Summary
BugTrace is an AI software-incident investigator built on real SWE-bench Verified
issues. A single LangGraph agent investigates a repository checked out at the pre-fix
commit. It uses read-only MCP tools, ranks suspect code, and writes a root-cause report in
which every claim cites tool-returned evidence. It then proposes a patch that a human
must approve before anything is written. The approved patch can be graded by the official
SWE-bench harness behind a tested boundary that keeps benchmark answers away from the
agent. The project ships with a FastAPI and SSE investigation UI, SQLite persistence with
resumable approval checkpoints, a deterministic regression suite, CI, and a configurable
Gemini API or Vertex AI backend with a Cloud Run deployment path.

What it demonstrates: agent orchestration with hard bounds, tool-surface security,
evidence grounding, a human-in-the-loop gate, evaluation hygiene (no answer leakage), and
honest reporting of what has and has not been validated.

## CV bullets
- Built BugTrace, an evidence-grounded AI bug investigator (Python, LangGraph, MCP,
  FastAPI, Gemini/Vertex AI) that localizes root causes in real SWE-bench Verified
  repositories. It accepts a root-cause claim only when the claim cites evidence IDs
  returned by sandboxed, read-only repository tools.
- Designed a human-approval mutation gate. Model-proposed diffs pass strict validation
  and a `git apply --check` run, then a separate privileged MCP server applies only the
  approved diff, bound by its sha256. A 10-scenario deterministic regression suite in CI
  checks for zero unauthorized writes.
- Enforced a strict benchmark-leakage boundary. Gold patches and tests are evaluator-only,
  and import-graph and payload tests keep them out of prompts, checkpoints, API
  responses and the UI. Patches are graded only by the pinned official SWE-bench harness.

These bullets describe implemented and tested behaviour. They make no claims about
resolution rates: no benchmark has been run (see the README status table).

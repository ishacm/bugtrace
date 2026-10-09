# TASK 008 — Patch Proposal & Human Approval

## Read first
- `docs/AGENT.md` V2 section
- `docs/MCP.md` privileged tools
- `docs/API.md` decision endpoint

## Goal
Add controlled mutation while proving that the LLM cannot change repository files without explicit human approval.

## Implement
- structured `ProposedPatch` model.
- patch-generation node using current evidence/root cause.
- unified diff output.
- persistent LangGraph pause/interrupt at approval boundary.
- `POST /api/investigations/{id}/decision` with approve/reject.
- privileged execution MCP server with `apply_patch` only after authorization check.
- `git apply --check` before `git apply`.
- store resulting diff/status.
- UI patch viewer and Approve & Test / Reject controls.

## Security tests
- direct call to mutation tool before approval fails.
- API decision in wrong state returns 409.
- reject does not modify repo.
- approval state survives restart.
- traversal protections still hold.

## Acceptance criteria
Patch generation is visible, but repository contents cannot change until user explicitly approves.

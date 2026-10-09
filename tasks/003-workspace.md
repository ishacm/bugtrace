# TASK 003 — Isolated Repository Workspace Manager

## Read first
- `docs/ARCHITECTURE.md`
- `docs/MCP.md` security rules

## Goal
Given a safe case, reproduce the repository at exactly its `base_commit` in an isolated per-investigation workspace.

## Implement
- investigation UUID creation if not already present.
- workspace path `var/workspaces/<investigation_id>/repo`.
- clone/fetch/checkout logic with timeout and clear errors.
- verify `git rev-parse HEAD` matches the expected base commit.
- shallow/filtered clone first; safe fallback if necessary.
- cleanup helper for stale workspace.
- path resolution helper proving paths remain inside workspace.

## Security
- no shell=True.
- argument arrays only.
- no command runs outside explicit cwd.
- reject path traversal.

## Tests
Use a tiny local Git fixture repository. Do not clone Django in ordinary CI.

Test:
- correct commit checkout.
- workspace isolation.
- traversal rejection.
- command timeout/error handling.

## Acceptance criteria
One real seeded case can be prepared manually, while automated tests remain fast using fixtures.

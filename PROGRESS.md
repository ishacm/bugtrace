# BugTrace Progress

## Current version
V1

## Current task
TASK 007 — Detective UI + live streaming

## Completed
- [x] TASK 001 — Bootstrap
- [x] TASK 002 — SWE-bench safe data adapter
- [x] TASK 003 — Repository workspace manager
- [x] TASK 004 — MCP repository tools
- [x] TASK 005 — LangGraph investigation agent
- [x] TASK 006 — API + persistence
- [ ] TASK 007 — Detective UI + live streaming
- [ ] TASK 008 — Patch proposal + human approval
- [ ] TASK 009 — SWE-bench evaluation
- [ ] TASK 010 — Regression evals + CI
- [ ] TASK 011 — Vertex AI + Cloud Run
- [ ] TASK 012 — Polish + optional V3 extension

## Tests
TASK 001: `make test` — 2 passed (health endpoint, config in test mode). `make lint` (ruff check + format check) clean. `make doctor` passes (warnings: `.env` missing, Docker not installed). `make dev` serves `GET /api/health` → `{"status":"ok"}`.
TASK 002: `make test` — 20 passed (leakage boundary: PublicCase whitelist + extra=forbid, adapter strips gold, seed output has no gold keys, store rejects leaked keys, `/api/cases` public-only, only `app/evaluation` may import gold models, only the adapter may import raw source). `make lint` clean. `make seed` wrote 10 real SWE-bench Verified cases to `data/cases.jsonl`; `/api/cases` lists 10.
TASK 003: `make test` — 65 passed (fixture-repo checkout of exact commit, idempotent re-prepare, per-investigation isolation, clean failure on unknown commit/unreachable remote, remote URL/sha/repo/investigation-id validation, no git walk-up into enclosing repo, stale cleanup, traversal + symlink-escape rejection, runner timeout/exit/missing-binary/arg validation/GIT_* env scrubbing, no `shell=True`). `make lint` clean. Manual: `make workspace CASE=astropy__astropy-12907` prepared `var/workspaces/<uuid>/repo` with HEAD == base_commit (shallow SHA fetch, ~5s).
TASK 004: `make test` — 93 passed (all 8 tools against fixture repo, traversal/symlink/.git/.env rejection, 300-line read cap, search cap, typed errors for missing/binary/invalid input, ref/glob injection rejected, deterministic evidence IDs, no-mutation snapshot, stdio MCP server end-to-end via `RepoMCPClient`, server refuses unprepared workspace). `make lint` clean. Manual: MCP client against `astropy__astropy-12907` workspace — find_symbol/read_file/search_code/list_test_files/git_log returned real evidence; traversal rejected; working tree unchanged.
TASK 005: `make test` — 111 passed (stub-model graph tests: end-to-end over stdio MCP on fixture repo → COMPLETED report with grounded claims, invented paths/IDs rejected, unsupported claim dropped; iteration bound; tool-call bound; no-new-leads stop; insufficient evidence triggers second search + revised hypothesis; model failures → PARTIAL/LOW; sufficiency rules; explicit suspect scoring; gold data absent from state/prompts/record; agent package has no gold/raw-dataset/workspace/repo_tools imports). `make lint` clean. Follow-up after first live run: added bounded retry/backoff for Gemini 429/5xx and fixed fallback root cause to force PARTIAL/LOW (was reporting COMPLETED/HIGH); `make test` — 114 passed. Live: `make investigate CASE=astropy__astropy-12907` with `gemini-3.8-flash` → COMPLETED, MEDIUM, 11 tool calls, 1 iteration; suspects `_coord_matrix`, `_cstack`, `_separable` in `astropy/modeling/separable.py`; 5 grounded claims, all with evidence IDs; 1 model call still failed with 503 after 4 attempts (fallback used); ~7 min wall time, mostly 503 backoff. Record: `var/investigations/fd62be3f-….json`. Not scored against gold (evaluation is TASK 009).

TASK 006: `make format`, `make lint` clean; `make test` — 151 passed (37 new, stub model + in-process repo tools, no Gemini: start → poll → UNRESOLVED with persisted suspects/evidence/report/timeline; POST returns PREPARING before the run finishes; unknown case 404; malformed/extra-field/traversal instance_id 422; missing id 404; non-UUID id 422; recursive no-gold-keys check on POST/GET; persistence across app restart (identical GET); fresh-store round trip; restart marks interrupted runs FAILED; shutdown marks running runs FAILED; workspace failure → FAILED + error event; legal/illegal transition table; store rejects illegal transition and keeps status; cancel → CANCELLED, second cancel/finished cancel → 409; event appends are not duplicated). Live (uvicorn, real Gemini): `POST /api/investigations` for `astropy__astropy-12907` returned `PREPARING` immediately; polling reached `UNRESOLVED` with report PARTIAL/LOW, 5 evidence records, 0 suspects, because every model call failed (one 503, then 429 quota exhausted) and fallbacks were used; after a server restart `GET` returned the same persisted state. A full live run with suspects and root cause still needs model quota.

## Blockers
None.

## Known limitations
- Docker not installed locally; doctor reports a WARNING (needed only for V2 evaluation).
- No LangGraph, MCP or model code yet (by design).
- Seed takes the first N rows of the split; the default 10 are all `astropy/astropy`.
- SWE-bench rows have no `issue_url`; field is `null` unless a source provides it.
- `hints_text` is excluded from `PublicCase` (issue comments may hint at the fix).
- Workspace fallback (blobless full fetch) is implemented but not exercised against GitHub; GitHub served the shallow SHA fetch directly.
- MCP SDK is 2.x (`MCPServer`, not `FastMCP`).
- `search_code`/`find_symbol` use `git grep` (tracked files only); `find_symbol` is a Python text heuristic (def/class/module-level assignment).
- Shallow workspaces have one commit, so `git_log`/`git_show` history is limited to the base commit.
- Evidence IDs are deterministic hashes of tool + args + result (`ev-<12 hex>`).
- Agent: `find_symbol`/search are Python-focused (`**/*.py`); test evidence comes from `search_code` in `**/test*.py`; git evidence is shallow (`git_log` on the base commit only), so HIGH confidence is easier to reach via test evidence than history.
- Persistence: SQLite at `BUGTRACE_DATABASE_PATH` (default `var/bugtrace.db`). Tables `investigations` (status, PublicCase, `InvestigationRecord` snapshot minus events, error) and `investigation_events` (append-only, `source` = agent|service). `make investigate` uses the same service/store and also exports the record JSON to `var/investigations/<id>.json`.
- Runs execute as in-process asyncio tasks (no queue). Runs that were in flight when the server stopped are marked FAILED on shutdown/next startup; they are not resumed.
- V1 has no patch stage, so a finished investigation ends `UNRESOLVED` (report status COMPLETED/PARTIAL is separate). TASK 008 will route through `AWAITING_APPROVAL`.
- Endpoints: `POST /api/investigations` (202), `GET /api/investigations/{id}` (includes `allowed_transitions` and full event timeline), `POST /api/investigations/{id}/cancel` (409 on illegal transition). No SSE or list endpoint yet (TASK 007).
- Agent snapshots are persisted after every graph node (`run_investigation(on_update=...)` via `astream`); the graph itself is unchanged. Added `repo_ready` event type.
- Live runs are currently limited by Gemini quota (429) and 503 overload.
- Model is configured via `BUGTRACE_MODEL_NAME` in `.env` (currently `gemini-3.8-flash`, chosen from the key's model listing as the newest stable non-preview text model). Gemini 503 overload is frequent; retries are bounded (4 attempts, 2/4/8s backoff) and failures degrade to fallbacks.
- Seeding uses the Hugging Face datasets-server REST API (network required).

## Notes
Update this file after every task. Do not record fabricated benchmark results.

## Next task
TASK 007 — Detective UI + live streaming

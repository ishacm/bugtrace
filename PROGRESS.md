# BugTrace Progress

## Current version
V1

## Current task
TASK 011 — Vertex AI + Cloud Run

## Completed
- [x] TASK 001 — Bootstrap
- [x] TASK 002 — SWE-bench safe data adapter
- [x] TASK 003 — Repository workspace manager
- [x] TASK 004 — MCP repository tools
- [x] TASK 005 — LangGraph investigation agent
- [x] TASK 006 — API + persistence
- [x] TASK 007 — Detective UI + live streaming
- [x] TASK 008 — Patch proposal + human approval
- [x] TASK 009 — SWE-bench evaluation
- [x] TASK 010 — Regression evals + CI
- [ ] TASK 011 — Vertex AI + Cloud Run
- [ ] TASK 012 — Polish + optional V3 extension

## Tests
TASK 001: `make test` — 2 passed (health endpoint, config in test mode). `make lint` (ruff check + format check) clean. `make doctor` passes (warnings: `.env` missing, Docker not installed). `make dev` serves `GET /api/health` → `{"status":"ok"}`.
TASK 002: `make test` — 20 passed (leakage boundary: PublicCase whitelist + extra=forbid, adapter strips gold, seed output has no gold keys, store rejects leaked keys, `/api/cases` public-only, only `app/evaluation` may import gold models, only the adapter may import raw source). `make lint` clean. `make seed` wrote 10 real SWE-bench Verified cases to `data/cases.jsonl`; `/api/cases` lists 10.
TASK 003: `make test` — 65 passed (fixture-repo checkout of exact commit, idempotent re-prepare, per-investigation isolation, clean failure on unknown commit/unreachable remote, remote URL/sha/repo/investigation-id validation, no git walk-up into enclosing repo, stale cleanup, traversal + symlink-escape rejection, runner timeout/exit/missing-binary/arg validation/GIT_* env scrubbing, no `shell=True`). `make lint` clean. Manual: `make workspace CASE=astropy__astropy-12907` prepared `var/workspaces/<uuid>/repo` with HEAD == base_commit (shallow SHA fetch, ~5s).
TASK 004: `make test` — 93 passed (all 8 tools against fixture repo, traversal/symlink/.git/.env rejection, 300-line read cap, search cap, typed errors for missing/binary/invalid input, ref/glob injection rejected, deterministic evidence IDs, no-mutation snapshot, stdio MCP server end-to-end via `RepoMCPClient`, server refuses unprepared workspace). `make lint` clean. Manual: MCP client against `astropy__astropy-12907` workspace — find_symbol/read_file/search_code/list_test_files/git_log returned real evidence; traversal rejected; working tree unchanged.
TASK 005: `make test` — 111 passed (stub-model graph tests: end-to-end over stdio MCP on fixture repo → COMPLETED report with grounded claims, invented paths/IDs rejected, unsupported claim dropped; iteration bound; tool-call bound; no-new-leads stop; insufficient evidence triggers second search + revised hypothesis; model failures → PARTIAL/LOW; sufficiency rules; explicit suspect scoring; gold data absent from state/prompts/record; agent package has no gold/raw-dataset/workspace/repo_tools imports). `make lint` clean. Follow-up after first live run: added bounded retry/backoff for Gemini 429/5xx and fixed fallback root cause to force PARTIAL/LOW (was reporting COMPLETED/HIGH); `make test` — 114 passed. Live: `make investigate CASE=astropy__astropy-12907` with `gemini-3.8-flash` → COMPLETED, MEDIUM, 11 tool calls, 1 iteration; suspects `_coord_matrix`, `_cstack`, `_separable` in `astropy/modeling/separable.py`; 5 grounded claims, all with evidence IDs; 1 model call still failed with 503 after 4 attempts (fallback used); ~7 min wall time, mostly 503 backoff. Record: `var/investigations/fd62be3f-….json`. Not scored against gold (evaluation is TASK 009).

TASK 006: `make format`, `make lint` clean; `make test` — 151 passed (37 new, stub model + in-process repo tools, no Gemini: start → poll → UNRESOLVED with persisted suspects/evidence/report/timeline; POST returns PREPARING before the run finishes; unknown case 404; malformed/extra-field/traversal instance_id 422; missing id 404; non-UUID id 422; recursive no-gold-keys check on POST/GET; persistence across app restart (identical GET); fresh-store round trip; restart marks interrupted runs FAILED; shutdown marks running runs FAILED; workspace failure → FAILED + error event; legal/illegal transition table; store rejects illegal transition and keeps status; cancel → CANCELLED, second cancel/finished cancel → 409; event appends are not duplicated). Live (uvicorn, real Gemini): `POST /api/investigations` for `astropy__astropy-12907` returned `PREPARING` immediately; polling reached `UNRESOLVED` with report PARTIAL/LOW, 5 evidence records, 0 suspects, because every model call failed (one 503, then 429 quota exhausted) and fallbacks were used; after a server restart `GET` returned the same persisted state. A full live run with suspects and root cause still needs model quota.

TASK 007: `make format`, `make lint` clean; `make test` — 169 passed (18 new, no Gemini: stub persisted investigations render COMPLETED/PARTIAL/UNRESOLVED-without-findings/FAILED/CANCELLED/PREPARING pages with distinct verdicts; empty states say "none identified" instead of inventing suspects/root causes; untrusted text (problem statement, event messages, evidence summaries) is escaped; 404/422 for missing/non-UUID ids; home lists seeded cases + case files and handles no seeded cases; static assets served; SSE replays every persisted event once, in order, with `id` = seq, then `state` + `end`; resume via `Last-Event-ID` (wins over `?after=`) and `?after=`; bad `Last-Event-ID`/`after` → 422; SSE over a live stub-model run equals the persisted timeline exactly and ends on the final state; the stream generator delivers non-terminal states while the run is in progress; a disconnected or cancelled stream leaves the run to finish with a report; a failed run streams its error event before `end`). Live (uvicorn, real seeded cases): home lists the 10 cases; the earlier TASK 006 investigation renders as PARTIAL FINDINGS with empty suspect/hypothesis states; a new `astropy__astropy-12907` run streamed 9 timeline events + 6 state updates and ended `UNRESOLVED` (report PARTIAL/LOW), matching `GET /api/investigations/{id}`. Every model call returned 429 (quota exhausted), so a live run with real suspects and a generated root cause has still not been shown in the UI. The stub-model SSE test covers rendering of a full COMPLETED report.

TASK 008: `make format`, `make lint` clean; `make test` — 207 passed (38 new in `tests/test_patches.py`, stub model, no Gemini). Coverage: diff validator accepts a valid diff, normalizes blank context lines, and rejects 17 malformed or unsafe diffs (empty, garbage, no hunks, wrong counts in either direction, bad hunk header, `..`/absolute paths, `.git`/`.env`, create/delete, rename, mode/binary headers, duplicate file, no-op hunk). Read-only `check_patch` never mutates and rejects stale context, missing files, traversal and symlink escapes. A COMPLETED report gives a grounded `ProposedPatch` (invented evidence ID dropped) and status AWAITING_APPROVAL with the workspace untouched. Invalid, non-applying or ungrounded diffs are retried with feedback up to `agent_max_patch_attempts`, then end UNRESOLVED with `patch_error`. A PARTIAL report proposes nothing. Reject → UNRESOLVED with the workspace byte-identical and HEAD unchanged. Approve → PATCHING → patch applied only in the workspace (one file modified, HEAD unchanged, BugTrace `git status` unchanged) → UNRESOLVED. Decisions get 409 in PREPARING/CANCELLED/finished-without-patch states and on a second decision; 404/422 for bad ids or bodies. AWAITING_APPROVAL and the patch survive an app restart, and approval then resumes the graph from the SQLite checkpoint. Direct `apply_patch` is refused before approval, when PATCHING has no recorded approval, and when the stored patch changed after approval; the stdio patch server exposes only `apply_patch` and refuses before approval. No `GOLD` text in any prompt (including patch prompts), API response or page. Checkpoint state round-trips with no unregistered-type warnings. Pages show the diff, the Approve/Reject controls only while AWAITING_APPROVAL, and rejected/applied/no-valid-patch outcomes. Live (uvicorn, `astropy__astropy-12907`): every Gemini call returned 429, so the report was PARTIAL/LOW and correctly no patch was proposed; a live `decision` on it returned 409 and the workspace was clean. A live end-to-end patch proposal has not been run yet (needs model quota).

TASK 008 follow-up (context enforcement, before TASK 009): removed `--unidiff-zero` from `git apply --check` and `git apply`. The validator now requires 3-line context (boundary exceptions above) and rejects zero-context and unanchored add-only hunks. The patch prompt asks for standard 3-line context. Test diffs now use standard context. `make format`, `make lint` clean; `make test` — 219 passed (12 new: zero-context, short-leading and unanchored-add rejection in the parser; check and real privileged apply for a 3-line-context patch, a line-1 patch, a last-line patch and an anchored insertion; short trailing context away from EOF rejected by `check_patch`; context-free model diffs never reach AWAITING_APPROVAL; `GIT_APPLY_OPTIONS` has no `--unidiff-zero`). Traversal, `.git`/`.env`, create/delete/rename, binary and grounding tests unchanged and passing.

TASK 009: `make format`, `make lint` clean; `make test` — 254 passed, 2 skipped (the opt-in real-case integration tests). 35 new tests, no Docker, network or Gemini. `tests/test_evaluation.py` runs a fake harness (`tests/fixtures/fake_swebench_harness.py`) as a real subprocess. It uses the same CLI and report layout as SWE-bench, and the test reference rows contain `GOLD` markers. Coverage:
- Predictions are written as SWE-bench JSONL. The harness command is an argument list with the pinned version, and its environment excludes `GEMINI_API_KEY`, `BUGTRACE_*` and `GIT_*`.
- Reference files are read from diff headers. Localization covers top-1 hit/miss, top-3 recall and candidate overlap.
- The reference cache stores rows and rejects traversal and malformed ids.
- Report parsing covers resolved, unresolved, infra failure, missing report and harness failure. Test results are stored as counts, never names.
- Evaluator outcomes, each graded from harness output: RESOLVED; UNRESOLVED (tests fail); UNRESOLVED (patch did not apply); TIMEOUT (tests); TIMEOUT (whole harness, with the process group killed and confirmed gone); ERROR (crash); UNAVAILABLE (no Docker, localization still computed, harness never started); ERROR (reference unavailable, or base commit mismatch); ERROR WORKSPACE_UNSAFE (extra workspace file, harness never started).
- `build_request` refuses unless the status is EVALUATING with an approval, a sha256 match and an applied patch.
- Service/API flow: approve goes PATCHING → EVALUATING (visible in the API and on the page) → RESOLVED. The evaluator receives exactly the approved diff and sha.
- Evaluator outcomes UNRESOLVED, UNAVAILABLE or an exception all end the case UNRESOLVED, with the result persisted and exception text not exposed.
- A full fake-harness flow through the API ends RESOLVED. No `GOLD` text or gold keys appear in the API or page, the LangGraph checkpoint DB or the agent record.
- Cancelling during EVALUATING kills the harness. Results survive a restart. A restart during EVALUATING ends FAILED. `record_evaluation` requires EVALUATING.
- Leakage tests were tightened: the agent, MCP, workspace and data packages import nothing from `app.evaluation`. A subprocess probe shows that importing the agent and both MCP servers loads only `app.evaluation.models` (gold-free), not `gold`. Result and request models contain no gold field names.

`make eval-integration` (`SWEBENCH_INTEGRATION=1`): the real reference row for `astropy__astropy-12907` was fetched from `SWE-bench/SWE-bench_Verified` and cached. Its base commit matches the seeded case and it has the harness 5.x fields (passed). The oracle harness run was skipped because Docker is not installed. `uv run --isolated --with swebench==5.0.2 python -m swebench.harness.run_evaluation --help` succeeded and lists every flag BugTrace passes. A manual run of the real `SwebenchEvaluator` on the real case returned `UNAVAILABLE / DOCKER_UNAVAILABLE` with localization computed from the real reference row. That run used a hand-written suspect list, so it is not a benchmark result. **No real harness verdict (RESOLVED/UNRESOLVED) has been produced yet.** It needs Docker. Acceptance is met via the documented-limitation branch: every boundary is implemented and tested.

TASK 010: `make format`, `make lint` clean; `make test` — 295 passed, 2 skipped (the opt-in real-case tests). 41 new tests.

`tests/test_regression.py` (31 tests) runs the 10 scenarios in `tests/regression/scenarios.json` once per module. Each goes through the real API, service, store, graph, privileged patch tools and evaluator boundary, with the stub model, the fixture repo, and either a static evaluator or the fake harness subprocess. Scenarios: approved + RESOLVED / tests fail / evaluator unavailable / evaluator timeout / evaluator crash; fake harness resolved; fake harness apply-fail; rejected; invalid patch; partial report. It checks:
- Every status actually stored (wrapped store methods) equals the scenario's expected path, and every edge is in `TRANSITIONS`.
- RESOLVED comes only from a harness `resolved: true`.
- Claims, cited ids, suspects and patch evidence are all recorded evidence. The invented file and id are dropped.
- The workspace is clean while AWAITING_APPROVAL. Only approved files change, and only after approval. HEAD is unchanged, there are 0 unauthorized writes, the BugTrace tree is untouched, and each investigation has its own workspace.
- The sha256 matches across proposal, approval, evaluator request and result. A diff tampered with after approval is refused by `build_request`.
- No gold keys or text in API bodies, pages, SSE streams, LangGraph checkpoint bytes or agent records.
- After a restart, every GET is identical.
- SSE replays the persisted timeline in order, and its final `state` panels equal `render_panels(stored)`.
- Evaluator status/flag maps to case status and verdict (RESOLVED without `resolved: true` shows as EVALUATION ERROR and ends UNRESOLVED).
- The stub report's summary equals the committed baseline. Exit policy: regression → 1, unauthorized write → 2.
- Report metrics equal the persisted values. Localization appears only where the harness computed it.

`tests/test_ci.py` (10 tests):
- Normal CI has no secrets, Gemini, Docker or dispatch.
- The eval workflow is `workflow_dispatch` only.
- Secrets and inputs reach the shell only through step `env:`; there is no `set -x` and no echoing of the key.
- `Settings` never renders the key.
- `agent_eval.py` exits 3 without a key and before any model call.
- The live manifest has 1–3 seeded, safe ids. The manifest rejects answer fields and traversal ids.
- The stub baseline is a produced summary.

A new autouse fixture removes `GEMINI_API_KEY` from every test.

Ran 4× back to back: deterministic. Workflows validated with `actionlint` 1.7.12 (no findings). A clean-checkout simulation (fresh git repo, no `.env`, no venv, no key, `env -i`, CI env vars) ran `uv sync --locked`, `make lint` and `make test`: 295 passed, 2 skipped, and it wrote `var/evals/regression.json`. The workflows have not run on GitHub yet: this repository has no remote.

Stub baseline (`evals/baselines/stub.json`): produced by the deterministic stub run, not a model benchmark. 10 scenarios; 9 COMPLETED reports; 8 patches proposed; 7 applied; 4 graded; 2 resolved; top-1 1.0 and top-3 0.5 (fixture reference only); grounding 1.0; 0 unauthorized writes; 50 tool calls. **No live baseline exists. No live agent evaluation has been run** (needs the Gemini key/quota and, with `--approve-patches`, Docker).

## Blockers
None. Real SWE-bench grading needs Docker, which is not installed here.

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
- Endpoints: `POST /api/investigations` (202), `GET /api/investigations/{id}` (includes `allowed_transitions` and full event timeline), `POST /api/investigations/{id}/cancel` (409 on illegal transition). `GET /api/investigations/{id}/events` (SSE) added in TASK 007; no JSON list endpoint (the homepage reads the store directly).
- Agent snapshots are persisted after every graph node (`run_investigation(on_update=...)` via `astream`); the graph itself is unchanged. Added `repo_ready` event type.
- Live runs are currently limited by Gemini quota (429) and 503 overload.
- Model is configured via `BUGTRACE_MODEL_NAME` in `.env` (currently `gemini-3.8-flash`, chosen from the key's model listing as the newest stable non-preview text model). Gemini 503 overload is frequent; retries are bounded (4 attempts, 2/4/8s backoff) and failures degrade to fallbacks.
- Seeding uses the Hugging Face datasets-server REST API (network required).
- UI: Jinja2 templates (`app/templates/`), vanilla JS + CSS (`app/static/`), routes in `app/web/` (`/`, `/investigations/{id}`, SSE). The page and the stream render through the same Jinja partials; the SSE `state` message carries server-rendered panel HTML, so the JS has no rendering logic of its own.
- SSE polls SQLite every `BUGTRACE_STREAM_POLL_SECONDS` (default 0.5s) and is a read-only view: the run is an independent asyncio task, so disconnects don't affect it. Every connection starts with a full state snapshot. Event ids are the persisted event sequence numbers. Keep-alive comment every 15s. One poll loop per connected client.
- `store.transition(..., event=...)` writes the status change and its lifecycle event in one transaction (used for FAILED), so a stream never reports FAILED before the error event is stored. Cancellation adds no timeline event; the status panel shows it.
- Starlette's TestClient buffers streaming responses, so tests for incremental delivery and disconnects drive the stream generator directly against a real `InvestigationService`.
- No authentication or CSRF protection on the UI or API: local single-user tool for V1.
- Patch stage (TASK 008). The graph continues after `finalize` only for a COMPLETED report: `propose_patch` → `pause_for_approval` (LangGraph `interrupt`) → `apply_patch` on approve, END on reject. Graph state is checkpointed with `AsyncSqliteSaver` at `BUGTRACE_CHECKPOINT_PATH` (default `var/checkpoints.db`, thread id = investigation id). The serializer allowlists BugTrace's own Pydantic/enum types. CLI/test runs without a checkpointer use an in-memory saver.
- Patch validation: strict unified-diff parser (`app/agent/patches.py`), then a grounding check (every file must appear in evidence; at least one known evidence ID), then read-only `check_patch` (`git apply --check` with the patch file in a temp dir outside the repo). Only edits to existing files are allowed. Hunks need standard context: 3 unchanged lines before and after each change. Fewer leading lines are allowed only when the hunk starts at line 1; fewer trailing lines only when the hunk ends at the file's last line (checked against the file in `check_patch`). Add-only hunks with no context are rejected. Check and apply both use only `--whitespace=nowarn`, so git keeps its default strict anchoring. An earlier `--unidiff-zero` was removed before TASK 009 (follow-up below).
- Privileged execution: separate stdio MCP server `app/mcp_servers/patch_server.py` with a single tool `apply_patch()` that takes no diff. It loads the persisted patch and refuses unless status is PATCHING, an `approve` row exists in `investigation_approvals`, and the patch sha256 matches the approved one. It writes the patch to `var/workspaces/<id>/patches/<sha>.diff`, runs `git apply --check` then `git apply`, and records `git diff`/`--stat`. It never commits or pushes. The service starts it only after an approve decision.
- Decisions: `POST /api/investigations/{id}/decision` `{"decision": "approve"|"reject"}`. The store records the decision, changes status and adds the lifecycle event in one transaction (approve → PATCHING, reject → UNRESOLVED), then the paused graph is resumed in the background. Added transition PATCHING → UNRESOLVED (applied, not evaluated). TASK 009 should route through EVALUATING instead.
- New event types: `patch_proposed`, `approval_required`, `patch_approved`, `patch_rejected`, `patch_applied`. A finished investigation with no valid patch ends UNRESOLVED with `patch_error` and a `NO VALID PATCH` event.
- The UI panel is `proposal`, not `patch`, because `patch` is a SWE-bench gold key name and the no-gold-keys tests treat it as a leak. The button reads "Approve & apply" rather than the task's "Approve & Test" because evaluation arrives in TASK 009.
- The read-only repo server now has 9 tools (added `check_patch`, which never writes).

- Evaluation (TASK 009). After an approved patch is applied, `InvestigationService._evaluate` moves PATCHING → EVALUATING and builds an `EvaluationRequest` (`app/evaluation/evaluator.build_request`). That step re-checks the approval, the sha256 binding and the applied result, and reads `git status` of the workspace for the safety check. It then calls the `Evaluator`. `record_evaluation` stores the result in `investigation_evaluations`, appends the event and moves to RESOLVED in one transaction. RESOLVED requires the harness's own `resolved: true`. Every other outcome (failed tests, apply failure, error, timeout, unavailable) ends UNRESOLVED, and `evaluation.status` tells them apart. A failed or refused apply still goes PATCHING → UNRESOLVED without evaluation.
- Evaluation is not an MCP tool, unlike `docs/MCP.md`'s V2 sketch. It runs in the service, outside the agent's tool surface, so neither the graph nor any MCP server can reach reference data. The approval re-check that the doc asks for is in `build_request`.
- Reference data: `GoldSource` reads `SWE-bench/SWE-bench_Verified` (`BUGTRACE_EVALUATION_DATASET`). The seed dataset `princeton-nlp/...` lacks the harness 5.x fields (`image`, `eval_script`, `log_parser`). Rows are loaded by paging datasets-server `/rows`, because `/filter` returned 500. They are cached at `var/evaluations/reference/<instance_id>.json`. A base-commit mismatch with the public case is an ERROR.
- Harness: official `swebench.harness.run_evaluation`, pinned (`BUGTRACE_EVALUATION_HARNESS_VERSION=5.0.2`). It runs via `uv run --isolated --no-project --with swebench==…`, so its dependencies never enter BugTrace's env. It is an asyncio subprocess with an argument list, explicit cwd, its own process group, and a whole-run timeout (`BUGTRACE_EVALUATION_TIMEOUT_SECONDS`, default 3600). The per-test timeout is `BUGTRACE_EVALUATION_TEST_TIMEOUT_SECONDS` (default 1800). On a timeout, leftover containers named with the run id are removed (best effort).
- Run artifacts go in `var/evaluations/runs/<investigation_id>/<run_id>/`: `dataset.json` (the reference row), `predictions.jsonl`, `manifest.json` (argv + versions + hashes), `harness.log`, the harness `logs/` and `reports/`. These are evaluator-only and contain reference data. They are never served, persisted to the DB or shown. The persisted result keeps the run id, dataset, harness version, reference-row sha256, prediction sha256 and exit code for reproducibility.
- Persisted/API/UI result (`EvaluationResult`, gold-free) includes:
  - status, resolved, failure_reason and a fixed message (never stderr or log text)
  - patch applied in the evaluator
  - target/regression test pass/fail counts (FAIL_TO_PASS/PASS_TO_PASS counts, never names)
  - localization: top-1 file hit, top-3 file recall, reference file count, candidate overlap
  - trace: tool calls, iterations, evidence, grounded claims, agent seconds. Model calls and token usage are not recorded by the agent yet and are left null.
  - safety: changed files, unauthorized writes
- The API has a new `evaluation` field. The UI has a new `evaluation` panel and verdicts EVALUATING PATCH / CASE CLOSED / TESTS FAILED / NOT EVALUATED / EVALUATION ERROR / EVALUATION TIMED OUT. The approve button now reads "Approve & test".
- Agent state: removed the unused `test_result` slot. Evaluation results never enter graph state, checkpoints or the agent record, and evaluation never resumes the graph. The legal edge EVALUATING → AWAITING_APPROVAL (reopen) exists but is unused. If it is ever used, results must not be fed to the agent.
- Apple Silicon: the harness 5.x images are `x86_64` (e.g. `swebench/sweb.eval.x86_64.astropy_1776_astropy-12907`). On arm64 they need Docker Desktop's x86 emulation and will be slow. If an image cannot run, evaluation ends ERROR/TIMEOUT and the rest of BugTrace is unaffected.
- `fetch_raw_rows` now pages through `iter_raw_rows` with 100-row pages; the seed output is unchanged.

- CI (`.github/workflows/ci.yml`): push to main and PRs. `uv sync --locked`, `make lint` (ruff check + format check), `make test` (unit, fixture-repo integration, regression scenarios), and upload of `regression-report`. No secrets, Gemini, Docker or network-dependent tests. The real-case tests skip unless `SWEBENCH_INTEGRATION=1`.
- Manual evaluation (`.github/workflows/agent-eval.yml`, `workflow_dispatch` only). Job `swebench-integration` runs `make eval-integration` on a Docker-equipped runner with no secrets. Job `agent-eval` runs `scripts/agent_eval.py` over `evals/cases.json` (3 seeded astropy cases). It needs the `GEMINI_API_KEY` secret and a `model_name` input or the `BUGTRACE_MODEL_NAME` repo variable. It uploads `agent-eval.json` and the agent-safe investigation records. Reference/run dirs (`var/evaluations`) are not uploaded. Inputs reach the shell only via `env:` (no `${{ }}` in `run:`), and the secret is scoped to the steps that use it.
- The `approve_patches` dispatch input is treated as the human's explicit approval for that run. Each proposal is approved through the normal decision path (recorded and sha256-bound), applied only in its own workspace, then graded. Without it, proposals stay AWAITING_APPROVAL and nothing is applied.
- Reports (`app/evaluation/report.py`, gold-free): `RunReport` holds the mode (`stub`|`live`), config, per-case `CaseReport` and a `Summary` (completed reports, proposed/applied, evaluated/resolved, top-1 accuracy and top-3 recall over cases with localization, grounding rate, unauthorized writes, tool calls). Unmeasured metrics stay null.
- Baselines: `compare` flags a regression when a higher-is-better metric drops or unauthorized writes rise. Exit codes: 2 on any unauthorized write (always), 1 on a regression with `--fail-on-regression`. Baselines are stored as summary-only files (`evals/baselines/<mode>.json`) and promoted from produced reports with `scripts/promote_baseline.py`. Regenerate the stub baseline after an intended behaviour change with `make regression-baseline`.
- Unauthorized writes: any workspace change not in an approved and applied patch's `affected_files` (`git status` of the workspace, including untracked files).
- Live localization is computed by `reference_localization` (evaluator side) only after a case finishes.
- New make targets: `regression` (writes `var/evals/regression.json`), `regression-baseline`, `agent-eval` (`ARGS=...`).
- `REGRESSION_REPORT_PATH` (env) makes the regression suite write its JSON report.

## Notes
Update this file after every task. Do not record fabricated benchmark results.

## Next task
TASK 011 — Vertex AI + Cloud Run

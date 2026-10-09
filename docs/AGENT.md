# LangGraph Agent Design

## Goal
The agent should investigate before it patches. It must show how evidence changes its hypothesis.

## State
Include at least:
- `investigation_id`
- safe case data
- investigation plan
- evidence list
- suspects list
- hypotheses list
- current hypothesis
- searches performed
- files inspected
- tool call count
- iteration count
- evidence sufficiency
- root cause
- V2 proposed patch
- V2 approval status
- errors
- final report

## Evidence model
Each evidence record needs:
- stable evidence ID
- type (`issue`, `code`, `git`, `test`, `runtime`, optional `similar_incident`)
- source
- optional path/line range
- short excerpt
- summary
- relevance

## Suspect model
Each suspect needs:
- file path
- optional symbol
- relative confidence/rank
- reason
- evidence IDs

Do not invent precise probabilities before sufficient evidence. A normalized score/rank may be used for UI, but it must derive from explicit scoring logic rather than arbitrary LLM confidence.

## V1 graph

```text
START
  -> understand_issue
  -> create_plan
  -> search_repository
  -> inspect_candidates
  -> inspect_git_history / inspect_tests as useful
  -> formulate_hypothesis
  -> verify_evidence
       -> insufficient: search again (bounded)
       -> sufficient: produce_root_cause
  -> finalize
```

## Evidence sufficiency
A root cause requires at least:
- 2 independent evidence records
- at least 1 code evidence record
- explicit evidence IDs linked to the hypothesis

High confidence requires code evidence plus at least one test/runtime/git source and no unresolved major contradiction.

Use LOW/MEDIUM/HIGH rather than fake decimal certainty.

## V2 graph extension
As implemented (`app/agent/graph.py`):

```text
finalize
  -> report not COMPLETED: END (UNRESOLVED)
  -> propose_patch (up to `agent_max_patch_attempts`, with validator feedback)
       -> no valid patch: END (UNRESOLVED, patch_error)
  -> pause_for_approval (LangGraph interrupt, checkpointed in SQLite)
       -> reject: END
       -> approve: apply_patch -> END
```

Evaluation is deliberately not a graph node. After `apply_patch`, the service hands the
approved diff to the evaluator, so harness results and reference data never enter graph
state, checkpoints or prompts. A `revise_patch` loop after a failed evaluation is not
implemented: feeding test outcomes back to the agent would need its own leakage review.

## Prompt behavior
The system prompt must require:
- inspect before claiming;
- no invented files/symbols/tests;
- all factual root-cause claims grounded in evidence IDs;
- acknowledge conflicting evidence;
- prefer more evidence over unsupported certainty;
- no write action without approval.

## Loop limits
Default maximum:
- 4 investigation iterations
- 30 tool calls
- 2 patch attempts

If limits are reached, return a partial investigation with LOW confidence rather than looping indefinitely.

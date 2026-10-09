# SWE-bench Data & Leakage Boundary

## Dataset
Primary source: SWE-bench Verified.

The exact Hugging Face identifier can change across tooling examples, so centralize it in configuration instead of scattering it through the codebase.

## Agent-safe data
Create a `PublicCase` model containing only:
- `instance_id`
- `repo`
- `problem_statement`
- `base_commit`
- `issue_url` if available

## Evaluator-only data
Create a separate `GoldCase` model containing only evaluator fields such as:
- `instance_id`
- `patch`
- `test_patch`
- `fail_to_pass`
- `pass_to_pass`

## Hard rule
Agent modules must never import or receive `GoldCase` or gold patch/test metadata.

Do not pass a larger row and rely on prompts to ignore hidden answers. Construct a smaller safe object before the case enters the agent layer.

## Seeding
A seed script should load the source dataset and write a small local JSONL containing only safe case fields. Default to 10 cases for development.

## Evaluation isolation
The evaluator may load gold metadata by `instance_id` only after the agent has produced its output/patch.

## Incident memory V3
If adding vector memory, never store the current evaluation case's gold solution in the retrieval index. The memory set and evaluation set must be separated.

## Tests required
- `PublicCase` has no gold fields.
- Agent package has no import dependency on evaluator gold models.
- Seed output contains no gold keys.

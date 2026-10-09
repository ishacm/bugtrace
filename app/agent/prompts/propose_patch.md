Propose a minimal fix for this bug as a unified diff.

Root-cause report:
$root_cause

Suspects (ranked):
$suspects

Code evidence (each line is prefixed with its line number; the prefix is NOT part of the file):
$evidence
$feedback
Return:
- `summary`: one sentence describing the change.
- `rationale`: 2-4 sentences explaining why this change fixes the root cause.
- `diff`: a unified diff that applies with `git apply` at the repository root:
  - headers exactly `--- a/<path>` and `+++ b/<path>`, one pair per file;
  - only modify existing files that appear in the evidence above; do not create, delete or
    rename files;
  - hunk headers `@@ -<start>,<count> +<start>,<count> @@` with counts that match the hunk;
  - standard context: exactly 3 unchanged lines before and 3 after each change, copied
    exactly from the file and prefixed with a single space (blank lines become a single
    space). Use fewer only when the change is within 3 lines of the start or end of the
    file. Never emit a hunk with no context;
  - keep the change as small as possible; do not reformat unrelated code.
- `evidence_ids`: the evidence IDs that justify this change.

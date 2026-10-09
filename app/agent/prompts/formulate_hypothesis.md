Formulate (or revise) a root-cause hypothesis for this bug.

Issue analysis:
$analysis

Previous hypothesis:
$previous

Evidence records:
$evidence

Return:
- `statement`: the most likely root cause, grounded in the evidence.
- `evidence_ids`: IDs of the evidence supporting the statement.
- `suspects`: files/symbols likely responsible, each with supporting evidence IDs. Use only
  paths that appear in the evidence. Use an empty string for `symbol` if unknown.
- `contradictions`: evidence that conflicts with the hypothesis (empty if none).
- `follow_up_symbols` / `follow_up_queries`: what to search next if evidence is still weak
  (bare identifiers / literal strings; may be empty).

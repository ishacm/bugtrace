"""Agent-safe case models. Must never contain SWE-bench gold/grading fields."""

from pydantic import BaseModel, ConfigDict

# Raw dataset keys that are allowed to cross into the agent layer.
PUBLIC_CASE_FIELDS: frozenset[str] = frozenset(
    {"instance_id", "repo", "problem_statement", "base_commit", "issue_url"}
)


class PublicCase(BaseModel):
    """A SWE-bench case as the agent is allowed to see it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: str
    repo: str
    problem_statement: str
    base_commit: str
    issue_url: str | None = None

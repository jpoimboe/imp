"""Live verbs for the dispatchable subagent roles, read by the narrative builder to
label a running subagent on the status footer. A role absent from the map (including
the isolated-worktree role, which no engine/agents code may name) falls back to the
generic `subagent(<name>)` form built from runtime event data."""
from __future__ import annotations

SUBAGENT_VERBS: dict[str, str] = {
    "coder": "writing code",
    "code-reviewer": "reviewing code",
    "explorer": "exploring",
    "researcher": "researching",
}


def subagent_verb(role: str) -> str | None:
    """The live verb for a dispatchable subagent role, or None when unmapped."""
    return SUBAGENT_VERBS.get(role)

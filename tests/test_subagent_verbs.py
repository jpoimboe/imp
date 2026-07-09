from __future__ import annotations

from imp.agents.subagent_verbs import SUBAGENT_VERBS, subagent_verb


def test_each_dispatchable_role_has_its_verb() -> None:
    assert subagent_verb("coder") == "writing code"
    assert subagent_verb("code-reviewer") == "reviewing code"
    assert subagent_verb("explorer") == "exploring"
    assert subagent_verb("researcher") == "researching"
    assert set(SUBAGENT_VERBS) == {
        "coder", "code-reviewer", "explorer", "researcher"}


def test_unmapped_role_returns_none() -> None:
    assert subagent_verb("reviewer") is None            # a test fixture name, not a role
    assert subagent_verb("task-lead") is None           # a top-level step, not a subagent
    # the isolated-worktree role is intentionally absent: no engine/agents code may name
    # it (AC#4), so it falls back to the generic subagent(<name>) form.
    assert subagent_verb("spiker") is None

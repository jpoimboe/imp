from __future__ import annotations

import pytest


def test_raise_if_suspended_reraises_only_suspended() -> None:
    from imp.agents.subagents import SubagentSuspended, raise_if_suspended

    # Non-suspend exceptions are a no-op (caller falls through to is_error).
    raise_if_suspended(ValueError("x"))
    with pytest.raises(SubagentSuspended):
        raise_if_suspended(SubagentSuspended("paused"))


def test_subagent_suspended_is_reexported_from_agent_run() -> None:
    from imp.agents.subagents import SubagentSuspended as A
    from imp.engine.agent_run import SubagentSuspended as B
    assert A is B  # re-export, not a second class

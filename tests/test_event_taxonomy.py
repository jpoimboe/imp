from __future__ import annotations

from imp.agents.prompts import _EventsNamespace
from imp.engine.state import InMemoryEventLog


def test_agent_result_event_feeds_events_namespace() -> None:
    log = InMemoryEventLog()
    log.append(agent_id=None, type="step_started", payload={"step": "plan"})
    # The provider generator yields this control event; agent_run logs it verbatim.
    log.append(agent_id="step:x/plan", type="agent_result",
               payload={"status": "Success", "note": "done"})
    ns = _EventsNamespace(log)
    assert ns["plan"].status == "Success"
    assert ns["plan"].note == "done"


def test_no_agent_output_control_yields_remain() -> None:
    # Guards against a MISSED yield site (each provider logs its yields verbatim,
    # so a stray `agent_output` yield is never captured by the renamed consumer
    # and silently drops the deliverable). claude_code has THREE such yields.
    import inspect

    from imp.agents import dispatch
    from imp.providers import (
        anthropic,
        claude_code,
        codex_runner,
        openai,
        scripted,
    )

    for mod in (dispatch, anthropic, openai, claude_code, codex_runner, scripted):
        assert '"type": "agent_output"' not in inspect.getsource(mod), mod.__name__
    # the consumer no longer looks for the old type
    from imp.engine import agent_run
    assert 'event["type"] == "agent_output"' not in inspect.getsource(agent_run)

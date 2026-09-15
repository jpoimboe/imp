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
    # and silently drops the deliverable). claude has THREE such yields.
    import inspect

    from imp.agents import dispatch
    from imp.providers import (
        anthropic,
        claude,
        codex_runner,
        openai,
        scripted,
    )

    for mod in (dispatch, anthropic, openai, claude, codex_runner, scripted):
        assert '"type": "agent_output"' not in inspect.getsource(mod), mod.__name__
    # the consumer no longer looks for the old type
    from imp.engine import agent_run
    assert 'event["type"] == "agent_output"' not in inspect.getsource(agent_run)


def test_engine_message_and_user_message_with_subtype_and_step_context() -> None:
    from imp.agents.runner import Message, TextBlock
    from imp.engine.state import InMemoryEventLog

    log = InMemoryEventLog()
    em = log.append_engine_message(
        agent_id="step:x/plan",
        message=Message(role="system", content=(TextBlock(text="be terse"),)),
        subtype="system_prompt")
    assert em["type"] == "engine_message"
    assert em["payload"] == {
        "role": "system",
        "content": [{"type": "text", "text": "be terse"}],
        "subtype": "system_prompt"}

    um = log.append_user_message(
        agent_id="step:x/plan",
        message=Message(role="user", content=(TextBlock(text="hi"),)),
        subtype="reply")
    assert um["payload"]["subtype"] == "reply"

    sc = log.append_step_context(
        agent_id="step:x/plan", provider="p", model="m",
        tools_schema=[], params={}, max_tool_calls=1)
    assert "system_prompt" not in sc["payload"]


def test_required_keys_enforce_subtype_and_drop_system_prompt() -> None:
    import pytest

    from imp.agents.runner import Message, TextBlock
    from imp.engine.state import InMemoryEventLog

    strict = InMemoryEventLog(strict=True)
    # user_message without subtype is now invalid
    with pytest.raises(ValueError):
        strict.append(agent_id="a", type="user_message",
                      payload={"role": "user", "content": []})
    # engine_message without subtype is now invalid
    with pytest.raises(ValueError):
        strict.append(agent_id="a", type="engine_message",
                      payload={"role": "user", "content": []})
    # append_user_message rejects the removed `human` kwarg
    with pytest.raises(TypeError):
        strict.append_user_message(
            agent_id="a",
            message=Message(role="user", content=(TextBlock(text="x"),)),
            human=True)
    # step_context is valid with no system_prompt and rejects the removed kwarg
    ev = strict.append_step_context(
        agent_id="a", provider="p", model="m",
        tools_schema=[], params={}, max_tool_calls=1)
    assert "system_prompt" not in ev["payload"]
    with pytest.raises(TypeError):
        strict.append_step_context(
            agent_id="a", provider="p", model="m", tools_schema=[],
            params={}, max_tool_calls=1, system_prompt="s")


def test_append_agent_message_emits_agent_message_and_patch_merges() -> None:
    from imp.engine.state import InMemoryEventLog

    log = InMemoryEventLog()
    ev = log.append_agent_message(
        agent_id="step:x/plan", provider="anthropic", model="m",
        raw={"content": []}, usage={"input_tokens": 1}, stop_reason="end_turn")
    assert ev["type"] == "agent_message"
    log.update_last_agent_message_usage(
        agent_id="step:x/plan",
        usage={"output_tokens": 9}, context_tokens=1000)
    resp = next(e for e in log.read() if e["type"] == "agent_message")
    assert resp["payload"]["context_tokens"] == 1000
    assert resp["payload"]["usage"]["output_tokens"] == 9

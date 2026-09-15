from __future__ import annotations

from imp.cli import narrative as nv


def _ev(eid, etype, agent_id=None, **payload):
    return {"id": eid, "agent_id": agent_id, "type": etype, "payload": payload}


def _reading(eid, *, input_tokens, cache_read, context_window,
             context_tokens=None, agent_id="step:p/s", provider="claude_code"):
    return _ev(
        eid, "agent_message", agent_id=agent_id, provider=provider, model="m",
        stop_reason="end_turn",
        usage={"input_tokens": input_tokens, "output_tokens": 5,
               "cache_read_input_tokens": cache_read,
               "cache_creation_input_tokens": 0},
        context_tokens=context_tokens, context_window=context_window,
    )


def _good(eid, **kw):
    # prompt tokens = 60000 + 20000 = 80000; no provider reading of its own
    return _reading(eid, input_tokens=60000, cache_read=20000,
                    context_window=200_000, **kw)


def _empty(eid, **kw):
    # all-zero usage AND a zero provider reading — the freshly-resumed empty reply
    return _reading(eid, input_tokens=0, cache_read=0, context_tokens=0,
                    context_window=200_000, **kw)


def test_empty_reading_after_good_holds_the_gauge() -> None:
    b = nv.NarrativeBuilder()
    b.add(_good("a"))
    assert b.latest_context_tokens == 80_000
    assert b.latest_context_window == 200_000

    b.add(_empty("b"))
    # a non-informative all-zero reading must NOT regress the gauge to zero
    assert b.latest_context_tokens == 80_000
    assert b.latest_context_window == 200_000


def test_genuine_reading_after_empty_updates_the_gauge() -> None:
    b = nv.NarrativeBuilder()
    b.add(_good("a"))
    b.add(_empty("b"))
    # prompt tokens = 90000 + 10000 = 100000
    b.add(_reading("c", input_tokens=90_000, cache_read=10_000,
                   context_window=200_000))
    assert b.latest_context_tokens == 100_000
    assert b.latest_context_window == 200_000


def test_subagent_reading_never_moves_the_gauge() -> None:
    b = nv.NarrativeBuilder()
    b.add(_good("a"))
    # a sub:-prefixed reading is excluded from this builder's gauge
    b.add(_reading("s", input_tokens=99_000, cache_read=1_000,
                   context_window=200_000, agent_id="sub:tk1"))
    assert b.latest_context_tokens == 80_000
    assert b.latest_context_window == 200_000


def test_nested_subagent_builder_records_its_own_context() -> None:
    b = nv.NarrativeBuilder()
    b.add(_ev("st", "step_started", step="s", kind="agent", phase="p",
              model="m", provider="anthropic"))
    b.add(_good("a"))                                      # top-level gauge -> 80k
    b.add(_ev("tk1", "tool_call", agent_id="step:p/s", id="b1",
              name="sub_agent", input={"subagent_name": "coder"}))
    # The subagent's OWN reading (agent_id sub:tk1) is routed to the nested
    # builder, which records it as its own context.
    b.add(_reading("s2", input_tokens=99_000, cache_read=1_000,
                   context_window=200_000, agent_id="sub:tk1"))
    nested = b._sub_by_agent_id["sub:tk1"].nested
    assert nested.latest_context_tokens == 100_000     # nested records its own gauge
    # The top-level gauge is unmoved by the subagent's reading.
    assert b.latest_context_tokens == 80_000

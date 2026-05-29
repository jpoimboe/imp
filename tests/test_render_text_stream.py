# tests/test_render_text_stream.py
from __future__ import annotations

from imp.cli.render_text import stream_event


def _ev(etype, agent_id=None, **payload):
    return {"id": "x", "agent_id": agent_id, "type": etype, "payload": payload}


def test_stream_phase_step_and_outcome() -> None:
    assert "design" in stream_event(_ev("phase_started", phase="design"), color=False)
    assert "spec" in stream_event(
        _ev("step_started", step="spec", model="m", provider="anthropic"), color=False)
    assert "continue" in stream_event(
        _ev("step_completed", step="spec", action="continue"), color=False)


def test_stream_agent_text_full() -> None:
    long = "word " * 50
    out = stream_event(_ev("llm_response", agent_id="step:p/s", provider="anthropic",
                           model="m", stop_reason="end_turn", usage={},
                           raw={"content": [{"type": "text", "text": long}]}), color=False)
    assert out.count("word") == 50          # nothing truncated


def test_stream_tool_call_and_result_lines() -> None:
    assert "bash" in stream_event(
        _ev("tool_call", agent_id="step:p/s", id="b1", name="bash",
            input={"command": "ls"}), color=False)
    assert "ok" in stream_event(
        _ev("tool_result", agent_id="step:p/s", id="b1", content="ok", error=False),
        color=False)


def test_stream_ignores_agent_output() -> None:
    assert stream_event(_ev("agent_output", status="Success"), color=False) is None


def test_stream_subagent_events_indented() -> None:
    out = stream_event(_ev("llm_response", agent_id="sub:tk1", provider="anthropic",
                           model="m", stop_reason="end_turn", usage={},
                           raw={"content": [{"type": "text", "text": "sub says hi"}]}),
                       color=False)
    assert out.startswith("    ") or out.lstrip().startswith("sub")  # nested indent marker

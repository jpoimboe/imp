# tests/test_live_driver.py
from __future__ import annotations

import io

from imp.cli import narrative as nv
from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.cli.render_text import render_tree
from tests._event_fixtures import ev


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def _driver():
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    return drv, out


def _body(out: _Pipe) -> list[str]:
    return out.getvalue().splitlines()


def _render_body(events) -> list[str]:
    """Full render_tree output, INCLUDING the root header. The live driver emits
    its own `#N title` header on change_created (LiveDriver._emit_header), so
    `_body(out)` already carries that line as element [0] — the correct comparison
    is full-vs-full, not body-vs-body. The two headers are byte-identical here
    because these sequences feed no status-changing event, so render_tree's
    root.status stays "running" and its header gets no `(status)` suffix (the only
    part the design excludes from parity)."""
    tree = nv.build_tree(events)
    return list(render_tree(tree, full=False, color=False, width=80))


def test_burst_coalesces_once_on_flush() -> None:
    drv, out = _driver()
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "phase_started", phase="design"),
        ev("3", "step_started", step="spec", model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="b1", name="read_file", input={"p": "1"}),
        ev("5", "tool_result", agent_id="a", id="b1", content="ok"),
        ev("6", "tool_call", agent_id="a", id="b2", name="glob", input={"g": "*"}),
        ev("7", "tool_result", agent_id="a", id="b2", content="ok"),
    ]
    for e in events:
        drv.on_event(e)
    # Burst is still open (no non-tool event after it) — not emitted yet.
    assert not any("tools ·" in ln for ln in _body(out))
    drv.on_event(ev("8", "llm_response", agent_id="a",
                    raw={"content": [{"type": "text", "text": "done"}]},
                    usage={}))
    body = _body(out)
    burst_lines = [ln for ln in body if "tools ·" in ln]
    assert len(burst_lines) == 1 and "2 tools" in burst_lines[0]


def test_tree_order_burst_then_prose() -> None:
    drv, out = _driver()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "phase_started", phase="design"),
        ev("3", "step_started", step="spec", model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="b1", name="grep", input={"q": "x"}),
        ev("5", "tool_result", agent_id="a", id="b1", content="ok"),
        ev("6", "llm_response", agent_id="a",
           raw={"content": [{"type": "text", "text": "the answer"}]}, usage={}),
    ]:
        drv.on_event(e)
    body = _body(out)
    burst_i = next(i for i, ln in enumerate(body) if "tools ·" in ln)
    prose_i = next(i for i, ln in enumerate(body) if "the answer" in ln)
    assert burst_i < prose_i


def test_subagent_fold_does_not_leak_nested_children() -> None:
    drv, out = _driver()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "phase_started", phase="design"),
        ev("3", "step_started", step="spec", model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="t1", name="task",
           input={"subagent_name": "coder"}),
        ev("5", "tool_call", agent_id="sub:4", id="n1", name="edit_file",
           input={"path": "secret.py"}),
        ev("6", "tool_result", agent_id="sub:4", id="n1", content="ok"),
        ev("7", "tool_result", agent_id="a", id="t1", content="done"),
    ]:
        drv.on_event(e)
    body = "\n".join(_body(out))
    assert "task → coder" in body
    assert "secret.py" not in body  # nested burst must NOT leak into the stream


def test_depth_when_step_completed_flushes_trailing_burst() -> None:
    # A burst flushed by step_completed must render at its append-time depth
    # (under the step), not the shallower post-pop depth.
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "phase_started", phase="design"),
        ev("3", "step_started", step="spec", model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="b1", name="read_file", input={"p": "1"}),
        ev("5", "tool_result", agent_id="a", id="b1", content="ok"),
        ev("6", "step_completed", step="spec", action="continue"),
        ev("7", "phase_completed", phase="design"),
    ]
    drv, out = _driver()
    for e in events:
        drv.on_event(e)
    drv.finish()
    assert _body(out) == _render_body(events)


def test_body_parity_with_render_tree() -> None:
    events = [
        ev("1", "change_created", change_id=1, title="add auth"),
        ev("2", "phase_started", phase="design"),
        ev("3", "step_started", step="spec", model="m", provider="anthropic"),
        ev("4", "thinking", agent_id="a", content="real thinking here"),
        ev("5", "tool_call", agent_id="a", id="b1", name="read_file", input={"p": "1"}),
        ev("6", "tool_result", agent_id="a", id="b1", content="ok"),
        ev("7", "llm_response", agent_id="a",
           raw={"content": [{"type": "text", "text": "prose"}]}, usage={}),
        ev("8", "step_completed", step="spec", action="continue"),
        ev("9", "phase_completed", phase="design"),
    ]
    drv, out = _driver()
    for e in events:
        drv.on_event(e)
    drv.finish()
    assert _body(out) == _render_body(events)


def test_empty_thinking_dropped_in_live_path() -> None:
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "phase_started", phase="design"),
        ev("3", "step_started", step="spec", model="m", provider="anthropic"),
        ev("4", "thinking", agent_id="a", content="   "),
        ev("5", "step_completed", step="spec", action="continue"),
        ev("6", "phase_completed", phase="design"),
    ]
    drv, out = _driver()
    for e in events:
        drv.on_event(e)
    drv.finish()
    assert not any("thinking" in ln for ln in _body(out))
    assert _body(out) == _render_body(events)


def test_header_emitted_on_change_created() -> None:
    drv, out = _driver()
    drv.on_event(ev("1", "change_created", change_id=7, title="add auth"))
    assert _body(out)[0] == "#7  add auth"


from imp.cli.render_text import render_tree as _rt_full  # noqa: E402


def test_ask_user_emitted_before_input_callback_and_once() -> None:
    """The question must be visible when the prompt blocks. We model the engine's
    synchronous append-before-block: feeding the ask_user tool_call to on_event
    must emit the question block immediately, before any answer arrives."""
    drv, out = _driver()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "phase_started", phase="design"),
        ev("3", "step_started", step="spec", model="m", provider="anthropic"),
    ]:
        drv.on_event(e)
    before = "\n".join(_body(out))
    assert "Which port?" not in before
    drv.on_event(ev("4", "tool_call", agent_id="a", id="q1", name="ask_user",
                    input={"question": "Which port?"}))
    after = _body(out)
    q_lines = [ln for ln in after if "Which port?" in ln]
    assert len(q_lines) == 1                      # emitted exactly once, on append
    # The answer arrives later and must NOT be re-rendered as a node line live.
    drv.on_event(ev("5", "tool_result", agent_id="a", id="q1", content="8080"))
    assert not any("> 8080" in ln for ln in _body(out))


def test_human_gate_header_only_live_full_in_log() -> None:
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "phase_started", phase="design"),
        ev("3", "step_started", step="spec", model="m", provider="anthropic"),
        ev("4", "human_gate_pending", agent_id="a", prompt="Approve plan?",
           options=[{"label": "yes"}, {"label": "no"}]),
        ev("5", "gate_evaluated", agent_id="a", kind="human",
           action={"kind": "continue"}),
        ev("6", "step_completed", step="spec", action="continue"),
        ev("7", "phase_completed", phase="design"),
    ]
    drv, out = _driver()
    for e in events:
        drv.on_event(e)
    drv.finish()
    body = _body(out)
    # The pending state must NEVER appear; the resolved header must.
    assert not any("pending" in ln for ln in body)
    assert any("human gate → continue" in ln for ln in body)
    # Live trims the prompt/options recap...
    assert not any("Approve plan?" in ln for ln in body)
    # ...but imp log keeps the full node.
    log_body = list(_rt_full(nv.build_tree(events), full=False, color=False, width=80))
    assert any("Approve plan?" in ln for ln in log_body)


def test_human_gate_blocks_following_nodes_until_resolved() -> None:
    drv, out = _driver()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "phase_started", phase="design"),
        ev("3", "step_started", step="spec", model="m", provider="anthropic"),
        ev("4", "human_gate_pending", agent_id="a", prompt="ok?", options=[]),
        ev("5", "llm_response", agent_id="a",
           raw={"content": [{"type": "text", "text": "after gate"}]}, usage={}),
    ]:
        drv.on_event(e)
    # Gate unresolved -> nothing after it (including the prose) is emitted yet.
    assert not any("after gate" in ln for ln in _body(out))
    assert not any("human gate" in ln for ln in _body(out))
    drv.on_event(ev("6", "gate_evaluated", agent_id="a", kind="human",
                    action={"kind": "continue"}))
    body = _body(out)
    gate_i = next(i for i, ln in enumerate(body) if "human gate → continue" in ln)
    prose_i = next(i for i, ln in enumerate(body) if "after gate" in ln)
    assert gate_i < prose_i                       # tree order preserved


def test_agent_gate_children_render_at_depth_three() -> None:
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "phase_started", phase="design"),
        ev("3", "step_started", step="spec", model="m", provider="anthropic"),
        ev("4", "step_started", agent_id="a", kind="agent_gate", gate_stem="review"),
        ev("5", "gate_evaluated", agent_id="a", kind="agent",
           action={"kind": "continue"}),
        ev("6", "step_completed", step="spec", action="continue"),
        ev("7", "phase_completed", phase="design"),
    ]
    drv, out = _driver()
    for e in events:
        drv.on_event(e)
    drv.finish()
    assert _body(out) == _render_body(events)     # depth 3 outcome matches imp log


def test_finish_flushes_open_burst_at_pause() -> None:
    drv, out = _driver()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "phase_started", phase="design"),
        ev("3", "step_started", step="spec", model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="b1", name="read_file", input={"p": "1"}),
        ev("5", "tool_result", agent_id="a", id="b1", content="ok"),
    ]:
        drv.on_event(e)
    assert not any("tools ·" in ln for ln in _body(out))   # still open
    drv.finish()
    assert any("1 tools" in ln for ln in _body(out))       # flushed on finish

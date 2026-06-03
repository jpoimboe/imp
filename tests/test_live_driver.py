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


def test_burst_renders_nothing_in_live_path() -> None:
    drv, out = _driver()
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="b1", name="read_file", input={"p": "1"}),
        ev("5", "tool_result", agent_id="a", id="b1", content="ok"),
        ev("6", "tool_call", agent_id="a", id="b2", name="glob", input={"g": "*"}),
        ev("7", "tool_result", agent_id="a", id="b2", content="ok"),
    ]
    for e in events:
        drv.on_event(e)
    assert not any("tools" in ln for ln in _body(out))
    drv.on_event(ev("8", "llm_response", agent_id="a",
                    raw={"content": [{"type": "text", "text": "done"}]},
                    usage={}))
    body = _body(out)
    assert not any("tools" in ln for ln in body)   # burst dropped (A2)
    assert any("done" in ln for ln in body)        # prose still streams


def test_burst_dropped_prose_kept_in_order() -> None:
    drv, out = _driver()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="b1", name="grep", input={"q": "x"}),
        ev("5", "tool_result", agent_id="a", id="b1", content="ok"),
        ev("6", "llm_response", agent_id="a",
           raw={"content": [{"type": "text", "text": "the answer"}]}, usage={}),
    ]:
        drv.on_event(e)
    body = _body(out)
    assert not any("tools" in ln for ln in body)
    assert any("the answer" in ln for ln in body)


def test_subagent_descends_but_nested_burst_does_not_leak() -> None:
    drv, out = _driver()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="t1", name="task",
           input={"subagent_name": "coder"}),
        ev("5", "tool_call", agent_id="sub:4", id="n1", name="edit_file",
           input={"path": "secret.py"}),
        ev("6", "tool_result", agent_id="sub:4", id="n1", content="ok"),
        ev("7", "tool_result", agent_id="a", id="t1", content="done"),
    ]:
        drv.on_event(e)
    body = _body(out)
    # New boundary-header shape: ↳ glyph, no `· N events` suffix.
    assert any(ln.strip() == "↳ task → coder" for ln in body)
    assert not any("▸ task" in ln for ln in body)
    # The nested tool burst must still NOT leak into the stream (A2 filters it).
    assert not any("secret.py" in ln for ln in body)


def test_depth_when_step_completed_flushes_trailing_burst() -> None:
    # A burst flushed by step_completed must render at its append-time depth
    # (under the step), not the shallower post-pop depth.
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="b1", name="read_file", input={"p": "1"}),
        ev("5", "tool_result", agent_id="a", id="b1", content="ok"),
        ev("6", "step_completed", step="spec", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ]
    drv, out = _driver()
    for e in events:
        drv.on_event(e)
    drv.finish()
    assert _body(out) == _render_body(events)


def test_body_parity_with_render_tree() -> None:
    events = [
        ev("1", "change_created", change_id=1, title="add auth"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "thinking", agent_id="a", content="real thinking here"),
        ev("5", "tool_call", agent_id="a", id="b1", name="read_file", input={"p": "1"}),
        ev("6", "tool_result", agent_id="a", id="b1", content="ok"),
        ev("7", "llm_response", agent_id="a",
           raw={"content": [{"type": "text", "text": "prose"}]}, usage={}),
        ev("8", "step_completed", step="spec", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ]
    drv, out = _driver()
    for e in events:
        drv.on_event(e)
    drv.finish()
    assert _body(out) == _render_body(events)


def test_consecutive_prose_blank_separated_and_parity() -> None:
    # Two prose turns with a thinking-only turn between them must be blank-separated
    # live, exactly as in `imp log` — the live path streams prose one node at a time,
    # so this guards that its blank-line spacing matches the batch walk byte-for-byte.
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "llm_response", agent_id="a",
           raw={"content": [{"type": "text", "text": "First turn."}]}, usage={}),
        ev("5", "thinking", agent_id="a", content="hmm"),
        ev("6", "llm_response", agent_id="a",   # thinking-only → emits nothing
           raw={"content": [{"type": "thinking", "thinking": "hmm"}]}, usage={}),
        ev("7", "llm_response", agent_id="a",
           raw={"content": [{"type": "text", "text": "Second turn."}]}, usage={}),
        ev("8", "step_completed", step="spec", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ]
    drv, out = _driver()
    for e in events:
        drv.on_event(e)
    drv.finish()
    body = _body(out)
    second = next(i for i, ln in enumerate(body) if "Second turn." in ln)
    assert body[second - 1].strip() == ""        # blank separator between prose turns
    assert body == _render_body(events)          # live == imp log, byte-for-byte


def test_empty_thinking_dropped_in_live_path() -> None:
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "thinking", agent_id="a", content="   "),
        ev("5", "step_completed", step="spec", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ]
    drv, out = _driver()
    for e in events:
        drv.on_event(e)
    drv.finish()
    assert not any("thinking" in ln for ln in _body(out))
    assert _body(out) == _render_body(events)


def test_header_emitted_on_change_created() -> None:
    drv, out = _driver()
    drv.on_event(ev("1", "change_created", slug="add-auth-7", title="add auth"))
    assert _body(out)[0] == "#add-auth-7  add auth"


from imp.cli.render_text import render_tree as _rt_full  # noqa: E402


def test_human_gate_header_only_live_full_in_log() -> None:
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("3", "step_started", step="approve", kind="human", phase="design"),
        ev("4", "human_prompt_pending", agent_id="a", step="approve",
           phase="design", ask="Approve plan?",
           options=[{"label": "yes"}, {"label": "no"}]),
        ev("6", "step_completed", step="approve", kind="human",
           action={"verb": "continue"}, var_mutations={}),
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
        ev("3", "step_started", step="approve", kind="human", phase="design"),
        ev("4", "human_prompt_pending", agent_id="a", step="approve",
           phase="design", ask="ok?", options=[]),
    ]:
        drv.on_event(e)
    # Gate unresolved -> nothing after it is emitted yet.
    assert not any("human gate" in ln for ln in _body(out))
    drv.on_event(ev("6", "step_completed", step="approve", kind="human",
                    action={"verb": "continue"}, var_mutations={}))
    body = _body(out)
    gate_i = next(i for i, ln in enumerate(body) if "human gate → continue" in ln)
    outcome_i = next(i for i, ln in enumerate(body) if ln.strip() == "✓ approve")
    assert gate_i < outcome_i                      # tree order preserved


def test_open_burst_flushes_to_nothing_at_pause() -> None:
    drv, out = _driver()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="b1", name="read_file", input={"p": "1"}),
        ev("5", "tool_result", agent_id="a", id="b1", content="ok"),
    ]:
        drv.on_event(e)
    assert not any("tools" in ln for ln in _body(out))
    drv.finish()
    assert not any("tools" in ln for ln in _body(out))   # nothing to emit (A2)


def test_subagent_prose_streams_before_close() -> None:
    drv, out = _driver()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="t1", name="task",
           input={"subagent_name": "reviewer"}),
        ev("5", "llm_response", agent_id="sub:4",
           raw={"content": [{"type": "text", "text": "interim finding"}]}, usage={}),
    ]:
        drv.on_event(e)
    # Prose appears WHILE the subagent is still running (before the close event).
    body_before = _body(out)
    assert any("interim finding" in ln for ln in body_before)
    assert not any("→ status" in ln for ln in body_before)   # no outcome yet
    # Close the subagent; the outcome lands now.
    drv.on_event(ev("6", "tool_result", agent_id="a", id="t1",
                    content='{"status":"Success"}'))
    body_after = _body(out)
    assert any("→ status=Success" in ln for ln in body_after)


def test_subagent_outcome_column_matches_prose_column() -> None:
    drv, out = _driver()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="t1", name="task",
           input={"subagent_name": "reviewer"}),
        ev("5", "llm_response", agent_id="sub:4",
           raw={"content": [{"type": "text", "text": "finding"}]}, usage={}),
        ev("6", "tool_result", agent_id="a", id="t1",
           content='{"status":"Success"}'),
    ]:
        drv.on_event(e)
    body = _body(out)
    prose_line = next(ln for ln in body if "finding" in ln)
    outcome_line = next(ln for ln in body if "→ status=Success" in ln)

    def indent(s: str) -> int:
        return len(s) - len(s.lstrip())

    assert indent(prose_line) == indent(outcome_line)


def test_finish_force_pops_running_subagent_without_outcome() -> None:
    drv, out = _driver()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="t1", name="task",
           input={"subagent_name": "reviewer"}),
        ev("5", "llm_response", agent_id="sub:4",
           raw={"content": [{"type": "text", "text": "partial work"}]}, usage={}),
    ]:
        drv.on_event(e)
    # Subagent never closes; finish() must force-pop it without crashing.
    drv.finish()
    body = _body(out)
    assert any(ln.strip() == "↳ task → reviewer" for ln in body)
    assert any("partial work" in ln for ln in body)
    assert not any("→ status" in ln for ln in body)   # empty outcome → nothing


def test_subagent_body_parity_with_render_tree() -> None:
    # The stated invariant: snapshot (imp log) and live stay BYTE-IDENTICAL via the
    # shared header/outcome helpers. The subagent path is where the two renderers
    # diverge most (descended-container + outcome-on-pop vs. a single
    # _render_subagent call), so assert full-line parity for a subagent with prose
    # AND a structured outcome.
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="t1", name="task",
           input={"subagent_name": "reviewer"}),
        ev("5", "llm_response", agent_id="sub:4",
           raw={"content": [{"type": "text", "text": "the finding"}]}, usage={}),
        ev("6", "tool_result", agent_id="a", id="t1",
           content='{"status":"Success","summary":"ok"}'),
        ev("7", "step_completed", step="spec", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ]
    drv, out = _driver()
    for e in events:
        drv.on_event(e)
    drv.finish()
    assert _body(out) == _render_body(events)


# NOTE: there is intentionally NO doubly-nested (sub-subagent) live test. Routing
# is single-level (narrative.py:763-770) and spawned subagents get
# `subagent_defs={}`, so a sub-subagent's events misroute and the inner fold stays
# empty — the scenario is unreachable until the engine enables nesting. See the
# matching note in tests/test_render_text.py.


def test_nested_open_burst_emits_nothing_and_does_not_reorder() -> None:
    drv, out = _driver()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="t1", name="task",
           input={"subagent_name": "reviewer"}),
        # Nested burst left OPEN (no flushing event inside the subagent).
        ev("5", "tool_call", agent_id="sub:4", id="n1", name="bash", input={}),
        ev("6", "tool_result", agent_id="sub:4", id="n1", content="ok"),
        ev("7", "llm_response", agent_id="sub:4",
           raw={"content": [{"type": "text", "text": "after burst"}]}, usage={}),
    ]:
        drv.on_event(e)
    body = _body(out)
    assert not any("tools" in ln for ln in body)       # nested open burst emits nothing
    assert any("after burst" in ln for ln in body)     # prose still streams, in order
    # No duplicate prose line (guards against double-emit).
    assert sum("after burst" in ln for ln in body) == 1


def test_subagent_closing_with_open_nested_burst_still_emits_outcome() -> None:
    # A subagent can close (parent task tool_result) while a nested burst is still
    # the nested builder's current_burst (no trailing prose flushed it). The
    # builder-aware _is_final must NOT block the cursor on that burst once the
    # subagent has closed — the outcome must still emit on the normal (non-force)
    # walk. Regression guard for the _is_final builder-awareness fix.
    drv, out = _driver()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="t1", name="task",
           input={"subagent_name": "reviewer"}),
        ev("5", "tool_call", agent_id="sub:4", id="n1", name="bash", input={}),
        ev("6", "tool_result", agent_id="sub:4", id="n1", content="ok"),
        # Subagent closes with the nested burst still open (no flushing prose).
        ev("7", "tool_result", agent_id="a", id="t1",
           content='{"status":"Success"}'),
    ]:
        drv.on_event(e)
    body = _body(out)
    assert any(ln.strip() == "↳ task → reviewer" for ln in body)
    assert not any("tools" in ln for ln in body)        # nested burst dropped (A2)
    assert any("→ status=Success" in ln for ln in body)  # outcome emitted on close, non-force


def test_phase_b_streams_live_without_finish() -> None:
    """Real mid-run state: phase A completes, phase B starts and emits prose,
    and NO finish() is called. Phase B's header + prose must stream live. Before
    the _ensure_phase close-fix the cursor froze in phase A and phase B never
    appeared until finish()."""
    drv, out = _driver()
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("3", "step_completed", step="spec", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
        ev("4", "step_started", step="build", kind="agent", phase="implement",
           model="m", provider="anthropic"),
        ev("5", "llm_response", agent_id="a",
           raw={"content": [{"type": "text", "text": "building now"}]}, usage={}),
    ]
    for e in events:
        drv.on_event(e)
    # Deliberately NO drv.finish() — assert content streamed during the run.
    body = _body(out)
    assert any("implement" in ln for ln in body)    # phase B header streamed
    assert any("building now" in ln for ln in body)  # phase B prose streamed


def _driver_color():
    out = _Pipe()
    drv = LiveDriver(stream=out, color=True, width=80, status=LiveStatus(out))
    return drv, out


def test_live_subagent_prose_carries_gutter_byte_identical_to_log() -> None:
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="t1", name="task",
           input={"subagent_name": "reviewer"}),
        ev("5", "llm_response", agent_id="sub:4",
           raw={"content": [{"type": "text", "text": "the finding"}]}, usage={}),
        ev("6", "tool_result", agent_id="a", id="t1", content='{"status":"Success"}'),
    ]
    drv, out = _driver()
    for e in events:
        drv.on_event(e)
    drv.finish()
    body = _body(out)
    # The streamed `↳` header and the `│ ` gutter on prose + outcome…
    assert any(ln.strip() == "↳ task → reviewer" for ln in body)
    assert any(ln.endswith("│ the finding") for ln in body)
    # …and the whole body is byte-identical to the imp log replay (shared layer).
    assert body == _render_body(events)


def test_print_chrome_renders_plain_and_carries_no_gutter() -> None:
    drv, out = _driver_color()
    drv.print_chrome("hello from the agent")
    body = out.getvalue()
    assert "hello from the agent" in body
    assert "\x1b[" not in body   # agent = () → no codes; main-agent sink → no gutter


def test_user_message_suppressed_in_live_present_in_log() -> None:
    # §4: prompt_toolkit already echoed the user's input in live mode; re-rendering
    # the UserMessageNode would duplicate it. imp log has no terminal echo so it
    # must still render the node as the sole record.
    events = [
        ev("1", "change_created", change_id=1, title="t"),
        ev("2", "step_started", step="spec-writer", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        ev("3", "user_message", content=[{"type": "text", "text": "do the thing"}]),
    ]
    drv, out = _driver()
    for e in events:
        drv.on_event(e)
    drv.finish()
    live = out.getvalue()
    log = "\n".join(_render_body(events))

    assert "> do the thing" not in live   # suppressed in live (prompt_toolkit echoed)
    assert "> do the thing" in log        # the only record in imp log

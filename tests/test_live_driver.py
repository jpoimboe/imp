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


def test_burst_recap_streams_then_prose_in_live_path() -> None:
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
    assert not any("call" in ln for ln in _body(out))  # burst open — held
    drv.on_event(ev("8", "llm_response", agent_id="a",
                    raw={"content": [{"type": "text", "text": "done"}]},
                    usage={}))
    body = _body(out)
    # point 8: the closed burst leaves a single dark-gray recap line, then prose.
    assert any("read_file · glob · 2 calls" in ln for ln in body)
    assert any("done" in ln for ln in body)        # prose still streams after it


def test_burst_recap_then_prose_kept_in_order() -> None:
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
    recap_i = next(i for i, ln in enumerate(body) if "grep · 1 call" in ln)
    answer_i = next(i for i, ln in enumerate(body) if "the answer" in ln)
    assert recap_i < answer_i          # recap precedes the prose it preceded live


def test_live_subagent_only_status_rows() -> None:
    # Feed a subagent burst: live must show only the status row, never the
    # subagent's streamed children or the SDK sentinel outcome.
    drv, out = _driver()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="t1", name="task",
           input={"subagent_name": "reviewer"}),
        ev("5", "llm_response", agent_id="sub:4",
           raw={"content": [{"type": "text", "text": "some internal chatter"}]},
           usage={}),
        ev("6", "tool_result", agent_id="a", id="t1",
           content="The user doesn't want to proceed with this tool use..."),
    ]:
        drv.on_event(e)
    drv.finish()
    out_text = out.getvalue()
    assert "· reviewer" in out_text
    assert "some internal chatter" not in out_text
    assert "doesn't want to proceed" not in out_text


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
    # Part D: the subagent collapses to one resolved status row.
    assert any(ln.strip() == "· coder" for ln in body)
    assert not any("▸ task" in ln for ln in body)
    # The nested tool burst must still NOT leak into the stream.
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


def test_header_deferred_when_titleless() -> None:
    # Option B: a change created without a title opens with NO header rather than
    # a degenerate `#<slug>` line — the header waits until the change is named.
    drv, out = _driver()
    drv.on_event(ev("1", "change_created", slug="2026-06-03", title=""))
    assert out.getvalue() == ""  # nothing emitted yet


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
    outcome_i = next(i for i, ln in enumerate(body) if ln.strip() == "approve complete")
    assert gate_i < outcome_i                      # tree order preserved


def test_open_burst_recap_emitted_at_finish() -> None:
    drv, out = _driver()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="b1", name="read_file", input={"p": "1"}),
        ev("5", "tool_result", agent_id="a", id="b1", content="ok"),
    ]:
        drv.on_event(e)
    assert not any("call" in ln for ln in _body(out))   # still open — held
    drv.finish()
    # finish() force-flushes the open burst, leaving its recap (point 8).
    assert any("read_file · 1 call" in ln for ln in _body(out))


def test_subagent_row_resolves_only_on_close() -> None:
    # Part D: a running subagent is a blocking leaf — NOTHING (not even its row)
    # emits until it closes; its internal prose never appears. On close the single
    # resolved status row emits.
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
    # While running: internal prose hidden and no row yet (it resolves on close).
    body_before = _body(out)
    assert not any("interim finding" in ln for ln in body_before)
    assert not any("· reviewer" in ln for ln in body_before)
    # Close the subagent; the single resolved row lands now, with no outcome.
    drv.on_event(ev("6", "tool_result", agent_id="a", id="t1",
                    content='{"status":"Success"}'))
    body_after = _body(out)
    assert any(ln.strip() == "· reviewer" for ln in body_after)
    assert not any("status=Success" in ln for ln in body_after)


def test_subagent_status_row_column_matches_render_tree() -> None:
    # The collapsed status row sits at the same column in live and imp log.
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="t1", name="task",
           input={"subagent_name": "reviewer"}),
        ev("5", "llm_response", agent_id="sub:4",
           raw={"content": [{"type": "text", "text": "finding"}]}, usage={}),
        ev("6", "tool_result", agent_id="a", id="t1",
           content='{"status":"Success"}'),
    ]
    drv, out = _driver()
    for e in events:
        drv.on_event(e)
    drv.finish()
    body = _body(out)
    live_row = next(ln for ln in body if "· reviewer" in ln)
    log_row = next(ln for ln in _render_body(events) if "· reviewer" in ln)
    assert live_row == log_row


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
    # Subagent never closes; finish() force-emits its leaf without crashing. The
    # row shows the (still-running) status; internal prose and outcome stay hidden.
    drv.finish()
    body = _body(out)
    assert any(ln.strip() == "· reviewer (running)" for ln in body)
    assert not any("partial work" in ln for ln in body)
    assert not any("→ status" in ln for ln in body)   # no outcome ever


def test_subagent_body_parity_with_render_tree() -> None:
    # The stated invariant: snapshot (imp log) and live stay BYTE-IDENTICAL via the
    # shared subagent_status_row helper. Both now collapse the subagent to one
    # resolved status row, so assert full-body parity for a subagent that had
    # internal prose AND a structured outcome (neither of which must render).
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


def test_nested_subagent_io_never_leaks() -> None:
    # Part D: a subagent is a leaf — none of its internal I/O (nested tool bursts
    # or prose) ever reaches the live view, regardless of whether it closed.
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
    drv.finish()
    body = _body(out)
    assert not any("tools" in ln for ln in body)        # nested burst hidden
    assert not any("after burst" in ln for ln in body)  # internal prose hidden
    assert any("· reviewer" in ln for ln in body)  # only the status row


def test_subagent_closing_with_open_nested_burst_emits_only_status_row() -> None:
    # A subagent can close (parent task tool_result) while a nested burst is still
    # open. Part D: the close resolves the single status row on the normal (non-
    # force) walk; no nested I/O and no outcome render.
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
    assert any(ln.strip() == "· reviewer" for ln in body)
    assert not any("tools" in ln for ln in body)        # nested burst hidden
    assert not any("status=Success" in ln for ln in body)  # outcome hidden


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


def test_live_subagent_collapses_to_status_row_byte_identical_to_log() -> None:
    # Part D: a subagent is a LEAF — it collapses to one resolved status row, with
    # no streamed children, no gutter, and no outcome. The whole body is byte-
    # identical to the imp log replay (shared subagent_status_row layer).
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
    # The single resolved status row — no streamed prose, no gutter, no outcome.
    assert any(ln.strip() == "· reviewer" for ln in body)
    assert not any("the finding" in ln for ln in body)
    assert not any("│" in ln for ln in body)
    assert body == _render_body(events)


def test_print_chrome_renders_plain_and_carries_no_gutter() -> None:
    drv, out = _driver_color()
    drv.print_chrome("hello from the agent")
    body = out.getvalue()
    assert "hello from the agent" in body
    assert "\x1b[" not in body   # agent = () → no codes; main-agent sink → no gutter


def test_user_message_shown_in_nontty_live_suppressed_on_tty() -> None:
    # point 3: the interactive prompt must be visible in the transcript. On a real
    # terminal prompt_toolkit already echoed it, so the live driver suppresses the
    # node to avoid a duplicate. But a non-TTY sink (offline capture, `imp run |
    # tee`, the snap-ui mockup) has no echo, so there the node IS rendered — and
    # imp log always renders it.
    events = [
        ev("1", "change_created", change_id=1, title="t"),
        ev("2", "step_started", step="spec-writer", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        ev("2b", "awaiting_user_input", agent_id="step:spec/spec-writer"),
        ev("3", "user_message", content=[{"type": "text", "text": "do the thing"}]),
    ]
    # non-TTY live (the _Pipe sink reports isatty() == False)
    drv, out = _driver()
    for e in events:
        drv.on_event(e)
    drv.finish()
    assert "> do the thing" in out.getvalue()

    # TTY live: prompt_toolkit echoed the line, so suppress to avoid a dup.
    tout = _TTYPipe()
    tdrv = LiveDriver(stream=tout, color=False, width=80, status=LiveStatus(tout))
    for e in events:
        tdrv.on_event(e)
    tdrv.finish()
    assert "> do the thing" not in tout.getvalue()

    # imp log (no terminal echo) always renders the user turn.
    assert "> do the thing" in "\n".join(_render_body(events))


def test_final_open_phase_has_no_breadcrumb_in_live_or_log() -> None:
    """The final (never-closed) phase must NOT emit a <phase> complete breadcrumb in
    either the live path (finish()/force-pop) or the log (render_tree gated on
    node.closed). Uses distinct names for phase ("finalize") and step ("deploy")
    so a phase-level assert cannot accidentally match a step-level breadcrumb."""
    events = [
        {"type": "change_created", "agent_id": None, "payload": {"slug": "s", "title": "t"},
         "id": "e0", "timestamp": "2026-06-03T00:00:00+00:00"},
        {"type": "step_started", "agent_id": None,
         "payload": {"step": "deploy", "kind": "branch", "phase": "finalize"},
         "id": "e1", "timestamp": "2026-06-03T00:00:00+00:00"},
        {"type": "step_completed", "agent_id": None,
         "payload": {"step": "deploy", "kind": "branch", "action": {"verb": "continue"}},
         "id": "e2", "timestamp": "2026-06-03T00:00:10+00:00"},
        # No second step_started: the "finalize" phase is never closed.
    ]
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    for e in events:
        drv.on_event(e)
    drv.finish()
    live_lines = out.getvalue().splitlines()
    log_lines = list(render_tree(nv.build_tree(events), full=False, color=False, width=80))

    # The phase-level breadcrumb must be absent in BOTH renderers.
    assert not any("finalize complete ·" in ln for ln in live_lines), (
        f"phase breadcrumb leaked into live: {live_lines}")
    assert not any("finalize complete ·" in ln for ln in log_lines), (
        f"phase breadcrumb leaked into log: {log_lines}")
    # The step-level breadcrumb (deploy complete) may still appear — we don't assert on it.


def test_phase_breadcrumb_identical_in_live_and_log() -> None:
    events = [
        {"type": "change_created", "agent_id": None, "payload": {"slug": "s", "title": "t"},
         "id": "e0", "timestamp": "2026-06-03T00:00:00+00:00"},
        {"type": "step_started", "agent_id": None,
         "payload": {"step": "s1", "kind": "branch", "phase": "spec"},
         "id": "e1", "timestamp": "2026-06-03T00:00:00+00:00"},
        {"type": "step_completed", "agent_id": None,
         "payload": {"step": "s1", "kind": "branch", "action": {"verb": "continue"}},
         "id": "e2", "timestamp": "2026-06-03T00:00:05+00:00"},
        {"type": "step_started", "agent_id": None,
         "payload": {"step": "s2", "kind": "branch", "phase": "plan"},
         "id": "e3", "timestamp": "2026-06-03T00:00:05+00:00"},
        {"type": "step_completed", "agent_id": None,
         "payload": {"step": "s2", "kind": "branch", "action": {"verb": "continue"}},
         "id": "e4", "timestamp": "2026-06-03T00:00:09+00:00"},
    ]
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    for e in events:
        drv.on_event(e)
    drv.finish()
    live_line = next(ln for ln in out.getvalue().splitlines() if "spec complete" in ln)
    log_lines = render_tree(nv.build_tree(events), full=False, color=False, width=80)
    log_line = next(ln for ln in log_lines if "spec complete" in ln)
    assert live_line == log_line   # byte-identical indent + content across both paths


def test_live_prose_then_breadcrumb_blank_separated_and_parity() -> None:
    """Live path inserts a blank between prose and the step's BreadcrumbLine,
    mirroring render_text. Parity with the imp-log replay is preserved."""
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "llm_response", agent_id="a",
           raw={"content": [{"type": "text", "text": "the prose"}]}, usage={}),
        ev("5", "step_completed", step="spec", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ]
    drv, out = _driver()
    for e in events:
        drv.on_event(e)
    drv.finish()
    body = _body(out)
    bc_i = next(i for i, ln in enumerate(body) if "spec complete" in ln)
    assert body[bc_i - 1].strip() == ""
    assert body == _render_body(events)


def test_live_subagent_emits_no_internal_io_or_outcome() -> None:
    """Part D: a subagent collapses to one status row — its internal prose and the
    structured return value (the SDK cancellation-sentinel carrier) never reach the
    live view. The body stays byte-identical to the imp log replay."""
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="t1", name="task",
           input={"subagent_name": "reviewer"}),
        ev("5", "llm_response", agent_id="sub:4",
           raw={"content": [{"type": "text", "text": "the finding"}]}, usage={}),
        ev("6", "tool_result", agent_id="a", id="t1",
           content='{"status":"Success"}'),
    ]
    drv, out = _driver()
    for e in events:
        drv.on_event(e)
    drv.finish()
    body = _body(out)
    assert any(ln.strip() == "· reviewer" for ln in body)
    assert not any("the finding" in ln for ln in body)        # internal prose hidden
    assert not any("status=Success" in ln for ln in body)     # outcome hidden
    assert body == _render_body(events)


def test_live_prose_then_subagent_header_blank_separated() -> None:
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "llm_response", agent_id="a",
           raw={"content": [{"type": "text", "text": "Pre-task prose."}]}, usage={}),
        ev("tk", "tool_call", agent_id="a", id="t1", name="task",
           input={"subagent_name": "reviewer"}),
        ev("r", "tool_result", agent_id="a", id="t1",
           content='{"status":"Success"}'),
    ]
    drv, out = _driver()
    for e in events:
        drv.on_event(e)
    drv.finish()
    body = _body(out)
    hdr_i = next(i for i, ln in enumerate(body) if "· reviewer" in ln)
    assert body[hdr_i - 1].strip() == ""
    assert body == _render_body(events)


class _TTYPipe(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_refresh_status_writes_newline_after_prose_on_tty() -> None:
    """When the driver is about to draw a status line and the last permanent
    emission was prose, write a single \\n first so the status line sits
    below a blank separator. Then clear `_prev_prose` so the subsequent
    `status.hide()` from the next _print_lines doesn't re-add another blank."""
    out = _TTYPipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    # Set up a builder activity so current_activity() returns non-None.
    drv.on_event(ev("1", "change_created", change_id=1, title="x"))
    drv.on_event(ev("2", "step_started", step="spec", kind="agent", phase="p",
                    model="m", provider="anthropic"))
    out.truncate(0)
    out.seek(0)
    drv._prev_prose = True
    drv._refresh_status()
    written = out.getvalue()
    assert written.startswith("\n"), f"expected leading newline; got {written!r}"
    assert drv._prev_prose is False


def test_refresh_status_no_newline_on_non_tty_even_with_prev_prose() -> None:
    """On a non-TTY pipe the status line never draws, so injecting a \\n
    would create a phantom blank in scroll-back. The newline is gated on the
    stream's TTY state, inspected directly by the driver."""
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    drv.on_event(ev("1", "change_created", change_id=1, title="x"))
    drv.on_event(ev("2", "step_started", step="spec", kind="agent", phase="p",
                    model="m", provider="anthropic"))
    out.truncate(0)
    out.seek(0)
    drv._prev_prose = True
    drv._refresh_status()
    assert out.getvalue() == ""
    assert drv._prev_prose is True   # untouched on non-TTY


def test_header_emitted_on_rename_when_unnamed_at_create() -> None:
    # UNNAMED->RENAMED: the header (#slug  title) must appear after
    # change_renamed, never before.
    drv, out = _driver()
    drv.on_event(ev("1", "change_created", slug="", title=""))
    assert out.getvalue() == ""   # still nothing — unnamed
    drv.on_event(ev("2", "change_renamed", id="ch1",
                    old_title="", new_title="Add OAuth",
                    new_slug="2026-06-05-add-oauth"))
    body = _body(out)
    # The header line must be exactly the styled slug + title (not a FallbackLine).
    assert any(ln.startswith("#2026-06-05-add-oauth") and "Add OAuth" in ln
               for ln in body)


def test_header_emitted_exactly_once_when_named_from_start_then_renamed() -> None:
    # NAMED-FROM-START then renamed: the header must appear exactly once (no
    # duplicate on change_renamed).
    drv, out = _driver()
    drv.on_event(ev("1", "change_created", slug="2026-06-05-first",
                    title="First"))
    drv.on_event(ev("2", "change_renamed", id="ch1",
                    old_title="First", new_title="Second",
                    new_slug="2026-06-05-second"))
    body = _body(out)
    header_lines = [ln for ln in body if ln.startswith("#")]
    assert len(header_lines) == 1   # header emitted exactly once


def test_user_message_resets_status_span() -> None:
    """The status-line elapsed timer measures 'time the agent has been working
    since the last user reply' — across tools, thinks, and multiple
    llm_responses within one interactive turn. So `restart_span()` fires on
    `step_started` AND on `user_message`."""
    calls: list[str] = []

    class _CountingStatus(LiveStatus):
        def restart_span(self) -> None:
            calls.append("restart")
            super().restart_span()

    out = _Pipe()
    status = _CountingStatus(out)
    drv = LiveDriver(stream=out, color=False, width=80, status=status)
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="spec", kind="agent", phase="p",
           model="m", provider="anthropic"),
        ev("3", "tool_call", agent_id="a", id="b1", name="bash", input={}),
        ev("4", "tool_result", agent_id="a", id="b1", content="ok"),
        ev("5", "thinking", agent_id="a", content="hmm"),
        ev("6", "llm_response", agent_id="a",
           raw={"content": [{"type": "text", "text": "ask"}]}, usage={}),
        ev("7", "user_message", content=[{"type": "text", "text": "reply"}]),
        ev("8", "llm_response", agent_id="a",
           raw={"content": [{"type": "text", "text": "ack"}]}, usage={}),
    ]:
        drv.on_event(e)
    # restart_span fired exactly at step_started (e2) and user_message (e7).
    assert calls == ["restart", "restart"]

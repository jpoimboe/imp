# tests/test_live_driver.py
from __future__ import annotations

import inspect
import io
import json

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
    """Full render_tree output. Neither renderer emits a slug/title header any
    more (the transcript opens on the first turn), so the live body and the
    render_tree body are directly comparable line-for-line."""
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
    # The closed burst is final immediately; prose is held until step_completed
    # (F3 dedup: the trailing narration is held so agent_output can dedup it).
    assert any("2 calls · read_file · glob" in ln for ln in _body(out))
    drv.on_event(ev("9", "step_completed", step="spec", kind="agent",
                    action={"verb": "continue"}, var_mutations={}))
    body = _body(out)
    assert any("done" in ln for ln in body)        # prose streams at step_completed


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
        ev("7", "step_completed", step="spec", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ]:
        drv.on_event(e)
    body = _body(out)
    recap_i = next(i for i, ln in enumerate(body) if "1 call · grep" in ln)
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
    assert "reviewer" in out_text
    assert "· reviewer" not in out_text
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
    # Part D: the subagent collapses to one resolved status row (no leading bullet).
    assert any(ln.strip() == "coder" for ln in body)
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


def test_no_slug_title_header_emitted() -> None:
    # The slug/title banner is gone: change_created (and a later change_renamed)
    # emit no `#slug  title` line — the transcript opens on the first turn.
    drv, out = _driver()
    drv.on_event(ev("1", "change_created", handle="add-auth-7", title="add auth"))
    drv.on_event(ev("2", "change_renamed", id="ch1", old_title="add auth",
                    new_title="Add OAuth", handle="add-oauth-ch1"))
    body = _body(out)
    assert not any(ln.startswith("#") for ln in body)
    assert not any("add auth" in ln or "Add OAuth" in ln for ln in body)


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
    assert any("1 call · read_file" in ln for ln in _body(out))


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
    assert not any(ln.strip() == "reviewer" for ln in body_before)
    # Close the subagent; the single resolved row lands now, with no outcome.
    drv.on_event(ev("6", "tool_result", agent_id="a", id="t1",
                    content='{"status":"Success"}'))
    body_after = _body(out)
    assert any(ln.strip() == "reviewer" for ln in body_after)
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
    live_row = next(ln for ln in body if ln.strip().startswith("reviewer"))
    log_row = next(ln for ln in _render_body(events) if ln.strip().startswith("reviewer"))
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
    assert any(ln.strip() == "reviewer (running)" for ln in body)
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
    assert any(ln.strip().startswith("reviewer") for ln in body)  # only the status row


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
    assert any(ln.strip() == "reviewer" for ln in body)
    assert not any("tools" in ln for ln in body)        # nested burst hidden
    assert not any("status=Success" in ln for ln in body)  # outcome hidden


def test_phase_b_streams_live_without_finish() -> None:
    """Real mid-run state: phase A completes, phase B starts and emits prose,
    and NO finish() is called. Phase B's header must stream live immediately;
    prose streams at step_completed (F3 dedup hold). Before the _ensure_phase
    close-fix the cursor froze in phase A and phase B never appeared until finish()."""
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
    assert any("implement" in ln for ln in body)    # phase B header streamed live
    # Prose is held until step_completed (F3 dedup: trailing narration held for
    # agent_output dedup decision); it streams at step_completed, not finish().
    drv.on_event(ev("6", "step_completed", step="build", kind="agent",
                    action={"verb": "continue"}, var_mutations={}))
    body = _body(out)
    assert any("building now" in ln for ln in body)  # phase B prose at step_completed


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
    assert any(ln.strip() == "reviewer" for ln in body)
    assert not any("the finding" in ln for ln in body)
    assert not any("│" in ln for ln in body)
    assert body == _render_body(events)


def test_user_message_shown_in_live_on_tty_and_nontty() -> None:
    # The interactive prompt uses erase_when_done, so prompt_toolkit leaves no
    # echo on a real terminal — the live driver reprints the turn in BOTH the
    # TTY and non-TTY cases (and imp log always renders it). One shared path, so
    # the user's turn looks identical everywhere.
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

    # TTY live: prompt_toolkit erased its echo, so the driver reprints the turn.
    tout = _TTYPipe()
    tdrv = LiveDriver(stream=tout, color=False, width=80, status=LiveStatus(tout))
    for e in events:
        tdrv.on_event(e)
    tdrv.finish()
    assert "> do the thing" in tout.getvalue()

    # imp log (no terminal echo) always renders the user turn.
    assert "> do the thing" in "\n".join(_render_body(events))


def test_final_open_phase_has_no_breadcrumb_in_live_or_log() -> None:
    """The final (never-closed) phase must NOT emit a <phase> complete breadcrumb in
    either the live path (finish()/force-pop) or the log (render_tree gated on
    node.closed). Uses distinct names for phase ("finalize") and step ("deploy")
    so a phase-level assert cannot accidentally match a step-level breadcrumb."""
    events = [
        {"type": "change_created", "agent_id": None, "payload": {"handle": "s", "title": "t"},
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
        {"type": "change_created", "agent_id": None, "payload": {"handle": "s", "title": "t"},
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
    assert any(ln.strip() == "reviewer" for ln in body)
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
    hdr_i = next(i for i, ln in enumerate(body) if ln.strip().startswith("reviewer"))
    assert body[hdr_i - 1].strip() == ""
    assert body == _render_body(events)


class _TTYPipe(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_tty_chrome_steps_no_committed_status_no_double_blanks() -> None:
    """Regression (live-TTY): a chrome step's transient `· working` status line
    must NOT survive into scroll-back above its breadcrumb, and no two blank lines
    ever stack. Feed the byte stream through pyte (a faithful terminal) and assert
    the final screen: no `working` line committed, and no adjacent blank rows."""
    import pyte
    out = _TTYPipe()
    drv = LiveDriver(stream=out, color=False, width=60, status=LiveStatus(out))
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="spec-writer", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        ev("3", "llm_response", agent_id="a",
           raw={"content": [{"type": "text", "text": "Spec written."}]}, usage={}),
        ev("4", "step_completed", step="spec-writer", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
        ev("5", "step_started", step="spec-verify-commit", kind="script", phase="spec"),
        ev("6", "step_completed", step="spec-verify-commit", kind="script",
           action={"verb": "continue"}),
        ev("7", "step_started", step="fast-path-gate", kind="branch", phase="spec"),
        ev("8", "step_completed", step="fast-path-gate", kind="branch",
           action={"verb": "goto", "target": "plan-writer"}),
    ]:
        drv.on_event(e)
    drv.finish()
    screen = pyte.Screen(60, 30)
    pyte.Stream(screen).feed(out.getvalue().replace("\n", "\r\n"))
    rows = [ln.rstrip() for ln in screen.display]
    # The transient spinner line never gets committed to scroll-back.
    assert not any("working" in r for r in rows), f"committed status line: {rows}"
    # Each chrome step now prints both a header and a `complete` breadcrumb (§4).
    assert sum("spec-verify-commit" in r for r in rows) == 2
    assert sum("fast-path-gate" in r for r in rows) == 2
    # No two blank content rows in a row (trailing screen padding excluded).
    content = rows[:max(i for i, r in enumerate(rows) if r.strip()) + 1]
    assert not any(not content[i].strip() and not content[i + 1].strip()
                   for i in range(len(content) - 1)), f"double blank: {content}"


def test_refresh_status_injects_no_newline_and_preserves_prev_prose() -> None:
    """The status line is transient: _refresh_status draws it (a \\r-rewritten
    line, no newline) and must NOT inject a separating "\\n". That injected
    newline used to (a) commit the visible spinner line into scroll-back — a stray
    `name · working` above each breadcrumb — and (b) stack with _print_lines' own
    separator into double blanks. Permanent spacing is _print_lines' job alone, so
    `_prev_prose` is left untouched and the status writes no bare newline."""
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
    assert "\n" not in written, f"status must not inject a newline; got {written!r}"
    assert drv._prev_prose is True   # preserved → the NEXT block still gets one blank


def test_refresh_status_no_output_on_non_tty() -> None:
    """On a non-TTY pipe the status line never draws at all (it's TTY-only), so
    _refresh_status produces no output and leaves `_prev_prose` untouched."""
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
    assert drv._prev_prose is True   # untouched


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


def test_compose_status_colors_count_detail_label() -> None:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=True, width=80, status=LiveStatus(out))
    drv.on_event(ev("1", "step_started", step="s", kind="agent", phase="p"))
    drv.on_event(ev("t1", "tool_call", agent_id="a", id="c1", name="bash",
                    input={"command": "ls"}))
    line = drv.compose_status(0, 1.0)
    assert "1 call" in line
    assert "bash ls" in line
    assert "\x1b[90m" in line          # detail painted gray


def test_chrome_step_header_streams_live_and_matches_log() -> None:
    drv, out = _driver()
    events = [
        ev("2", "step_started", step="gate", kind="script", phase="verify"),
        ev("3", "step_completed", step="gate", kind="script",
           action={"verb": "continue"}, var_mutations={}),
    ]
    for e in events:
        drv.on_event(e)
    drv.finish()
    body = _body(out)
    assert any("gate" in ln and "complete" not in ln for ln in body)   # start header
    assert any("gate complete" in ln for ln in body)                    # breadcrumb
    assert body == _render_body(events)                                 # live == imp log


def test_prepare_for_prompt_flushes_separator_with_indent_bytes() -> None:
    drv, out = _driver()
    drv.on_event(ev("2", "step_started", step="s", kind="agent", phase="design",
                    model="m", provider="anthropic"))
    # step header emitted, _prev_prose armed for the step body
    drv.prepare_for_prompt()
    # the armed separator is a blank carrying the step-children indent (depth 1 = 2 spaces)
    assert out.getvalue().endswith("  \n")
    assert drv._separator_emitted is True


def test_deferred_print_lines_held_then_flushed_before_user_turn() -> None:
    drv, out = _driver()
    drv.on_event(ev("2", "step_started", step="s", kind="agent", phase="p",
                    model="m", provider="anthropic"))
    drv.prepare_for_prompt()
    base = out.getvalue()
    # a mid-prompt event (e.g. skill_load) must be DEFERRED while the prompt is active
    drv.on_event(ev("sk", "skill_load", agent_id="a", name="refactor", source="human"))
    assert out.getvalue() == base                       # nothing printed yet
    drv.flush_deferred()
    assert "load skill refactor" in out.getvalue()      # flushed at prompt exit


def test_flush_deferred_clears_prompt_active_for_liveness() -> None:
    drv, out = _driver()
    drv.prepare_for_prompt()
    drv.flush_deferred()
    assert drv._prompt_active is False                  # never left muted
    # subsequent events print normally
    drv.on_event(ev("2", "step_started", step="s", kind="agent", phase="p"))
    assert "s" in out.getvalue()


def test_wfu_submit_live_equals_imp_log() -> None:
    drv, out = _driver()
    events = [
        ev("2", "step_started", step="s", kind="agent", phase="p",
           model="m", provider="anthropic"),
        ev("aw", "awaiting_user_input", agent_id="a", prompt="continue?"),
        ev("t1", "tool_call", agent_id="a", id="w1", name="wait_for_user",
           input={"prompt": "continue?"}, timestamp="2026-06-10T00:00:01"),
        ev("t2", "tool_result", agent_id="a", id="w1", content="yes please"),
    ]
    for e in events:
        drv.on_event(e)
    drv.finish()
    assert _body(out) == _render_body(events)      # byte-identical to imp log


def test_prose_before_wait_for_user_emitted_before_the_prompt() -> None:
    # The narration in the SAME llm_response that calls wait_for_user is the
    # context the human must read to answer. It is created held=True (so a
    # trailing narration can fold into a step conclusion), but nothing un-held it
    # at the solicit boundary, so the live _walk blocked on it and the prose only
    # surfaced after the user replied (the next llm_response un-held it). Drive
    # events ONLY up to awaiting_user_input (the moment the prompt draws) — and do
    # NOT call finish(), which would force-flush and mask the stall — and assert
    # the prose is already visible.
    drv, out = _driver()
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="s", kind="agent", phase="p",
           model="m", provider="anthropic"),
        ev("3", "llm_response", agent_id="a",
           raw={"content": [{"type": "text",
                             "text": "Here are two options. Which do you prefer?"}]},
           usage={}),
        ev("4", "tool_call", agent_id="a", id="w1", name="wait_for_user",
           input={"prompt": "Which do you prefer?"}),
        ev("5", "awaiting_user_input", agent_id="a", prompt="Which do you prefer?"),
    ]
    for e in events:
        drv.on_event(e)
    assert any("Which do you prefer?" in ln for ln in _body(out)), (
        "pre-question narration must be emitted before the prompt draws, "
        f"not held until after the user answers; body={_body(out)}")


def test_live_driver_has_no_opening_question_suppression() -> None:
    params = inspect.signature(LiveDriver.__init__).parameters
    assert "suppress_opening_question" not in params


def test_quiet_continue_is_silent_live_and_matches_log() -> None:
    drv, out = _driver()
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="spec", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        ev("3", "llm_response", agent_id="step:spec/spec",
           raw={"content": [{"type": "text", "text": "done"}]}, usage={}),
        ev("4", "step_completed", step="spec", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
        ev("5", "step_started", step="spec-verify-commit", kind="script",
           phase="spec", quiet=True),
        ev("6", "step_completed", step="spec-verify-commit", kind="script",
           action={"verb": "continue"}, var_mutations={}),
    ]
    for e in events:
        drv.on_event(e)
    drv.finish()
    assert not any("spec-verify-commit" in ln for ln in _body(out))
    assert _body(out) == _render_body(events)   # live == imp log, line for line


def test_quiet_forward_goto_arrow_matches_log() -> None:
    drv, out = _driver()
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="fast-path-spec-gate", kind="branch",
           phase="spec", quiet=True),
        ev("3", "step_completed", step="fast-path-spec-gate", kind="branch",
           action={"verb": "goto", "target": "plan-writer", "resume": False},
           var_mutations={}),
        ev("4", "step_started", step="plan-writer", kind="agent", phase="plan",
           model="m", provider="anthropic"),
        ev("5", "llm_response", agent_id="step:plan/plan-writer",
           raw={"content": [{"type": "text", "text": "planning"}]}, usage={}),
        ev("6", "step_completed", step="plan-writer", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ]
    for e in events:
        drv.on_event(e)
    drv.finish()
    body = _body(out)
    assert any(ln.strip() == "→ plan" for ln in body)
    assert not any("fast-path-spec-gate" in ln for ln in body)
    assert body == _render_body(events)


def test_is_final_resume_goto_quiet_step_is_emitted() -> None:
    # A quiet step that closes on a backward rework edge (goto resume=True) sets
    # quiet_line="→ reworking" and is FINAL immediately — the live cursor must not
    # block on it the way it blocks an unresolved forward goto.
    drv, _ = _driver()
    node = nv.StepNode(name="spec-verify-commit", kind="script", quiet=True)
    node.quiet_line = "→ reworking"
    node.closed = True
    node.outcome = nv.OutcomeLine(
        action={"verb": "goto", "target": "spec-writer", "resume": True})
    assert drv._is_final(node, container=None) is True


def test_quiet_pause_surfaces_live_and_matches_log() -> None:
    # Live/log parity for the non-linear-outcome path: a quiet step that PAUSES
    # (merge_blocked etc.) must surface its full block in the live stream exactly
    # as `imp log` renders it — not get silently held/dropped like a quiet
    # continue.
    drv, out = _driver()
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="merge-to-base", kind="script",
           phase="merge", quiet=True),
        ev("3", "error", message="merge conflict in foo.py"),
        ev("4", "step_completed", step="merge-to-base", kind="script",
           action={"verb": "pause"}, var_mutations={}),
    ]
    for e in events:
        drv.on_event(e)
    drv.finish()
    body = _body(out)
    assert any("merge-to-base" in ln for ln in body)
    assert any("merge conflict in foo.py" in ln for ln in body)
    assert body == _render_body(events)          # live == imp log, line for line


def test_compose_activity_status_none_when_idle() -> None:
    from imp.cli.live_driver import compose_activity_status
    builder = nv.NarrativeBuilder()
    assert compose_activity_status(builder, frame=0, elapsed=1.0, color=True) is None


def test_compose_activity_status_paints_count_detail_label() -> None:
    from imp.cli.live_driver import compose_activity_status
    builder = nv.NarrativeBuilder()
    builder.add(ev("1", "step_started", step="s", kind="agent", phase="p"))
    builder.add(ev("t1", "tool_call", agent_id="a", id="c1", name="bash",
                   input={"command": "ls"}))
    line = compose_activity_status(builder, frame=0, elapsed=1.0, color=True)
    assert line is not None
    assert "1 call" in line
    assert "bash ls" in line
    assert "\x1b[90m" in line          # detail painted gray


def test_compose_status_returns_activity_content() -> None:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=True, width=80, status=LiveStatus(out))
    drv.on_event(ev("1", "step_started", step="s", kind="agent", phase="p"))
    drv.on_event(ev("t1", "tool_call", agent_id="a", id="c1", name="bash",
                    input={"command": "ls"}))
    result = drv.compose_status(0, 1.0)
    assert result is not None
    assert "bash ls" in result
    assert "1 call" in result
    assert "\x1b[90m" in result    # gray detail segment


def test_interrupt_breadcrumb_in_live_path() -> None:
    drv, out = _driver()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="spec-writer", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("3", "error", category="config", kind="interrupted",
           message="runner cancelled by interrupt"),
        ev("4", "step_completed", step="spec-writer", kind="agent",
           action={"verb": "pause"}, var_mutations={}),
    ]:
        drv.on_event(e)
    drv.finish()
    body = "\n".join(_body(out))
    assert "spec-writer interrupted" in body
    assert "complete · pause" not in body
    assert "error:" not in body


def test_loud_forward_goto_breadcrumb_held_until_phase_resolves() -> None:
    # A loud step (spec-reviewer) clean-approves with a plain forward goto to
    # plan-writer. Its breadcrumb route ("→ plan") is unknown until plan-writer's
    # step_started arrives, so the live driver must HOLD the breadcrumb and emit it
    # just before the new phase — matching imp log line-for-line.
    drv, out = _driver()
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="spec-reviewer", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        ev("3", "llm_response", agent_id="step:spec/spec-reviewer",
           raw={"content": [{"type": "text", "text": "approved"}]}, usage={}),
        ev("4", "step_completed", step="spec-reviewer", kind="agent",
           action={"verb": "goto", "target": "plan-writer",
                   "resume": False, "rework": False}, var_mutations={}),
        ev("5", "step_started", step="plan-writer", kind="agent", phase="plan",
           model="m", provider="anthropic"),
        ev("6", "llm_response", agent_id="step:plan/plan-writer",
           raw={"content": [{"type": "text", "text": "planning"}]}, usage={}),
        ev("7", "step_completed", step="plan-writer", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ]
    # Before plan-writer's step_started, the breadcrumb is held: no arrow yet.
    for e in events[:4]:
        drv.on_event(e)
    assert not any("→ plan" in ln for ln in _body(out))
    for e in events[4:]:
        drv.on_event(e)
    drv.finish()
    body = _body(out)
    assert any("spec-reviewer complete" in ln and "→ plan" in ln for ln in body)
    assert not any("goto(" in ln for ln in body)
    assert body == _render_body(events)   # live == imp log, line for line


def test_is_final_loud_reworking_breadcrumb_is_emitted() -> None:
    # A loud reviewer rework (goto + rework) is final immediately — the live cursor
    # must not block on it the way it blocks an unresolved forward goto.
    drv, _ = _driver()
    bc = nv.BreadcrumbLine(step_name="plan-review", elapsed_s=1.0,
                           action={"verb": "goto", "target": "plan-writer",
                                   "resume": False, "rework": True},
                           route=None)
    assert drv._is_final(bc, container=None) is True


def test_change_merged_suppressed_in_live_path() -> None:
    drv, out = _driver()
    for e in [
        ev("0", "change_created", id="run-x", handle="run-x",
           title="Run X"),
        ev("1", "step_started", step="merge-to-base", kind="merge", phase="review"),
        ev("2", "step_completed", step="merge-to-base", kind="merge",
           action={"verb": "continue"}, var_mutations={}),
        ev("3", "change_merged", base="master", commit="37dde900abc"),
    ]:
        drv.on_event(e)
    drv.finish()
    body = "\n".join(_body(out))
    # neither the raw event nor the friendly footer is emitted by the live driver
    # (the friendly line is owned by main.py _emit_completed).
    assert "change_merged" not in body
    assert "Merged 2026-06-12-run-x" not in body


def _panel_member_ev(eid: str, verdict: str, findings: str = "") -> list:
    bid = f"b{eid}"
    return [
        ev(f"c{eid}", "tool_call", agent_id="step:review/review", id=bid,
           name="task", input={"subagent_name": f"member-{eid}",
                               "task_description": "review"}),
        ev(f"r{eid}", "tool_result", agent_id="step:review/review", id=bid,
           content=json.dumps({"status": "Success", "verdict": verdict,
                               "findings": findings}), error=False),
    ]


def test_narration_un_held_after_agent_output_findings_hidden_live() -> None:
    # The live driver holds the trailing AgentTextNode until agent_output fires,
    # then un-holds it. findings is plumbing (E1) — hidden by default, not
    # promoted to prose. The narration is the single rendered copy.
    narration = "No findings. Spec is complete and ready for planning."
    findings = ("No findings. Spec is complete and ready for planning. "
                "All load-bearing claims were verified against the codebase.")
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="spec-reviewer", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        ev("3", "llm_response", agent_id="step:spec/spec-reviewer",
           provider="anthropic", model="m", usage={},
           raw={"content": [{"type": "text", "text": narration}]}),
        ev("4", "tool_call", agent_id="step:spec/spec-reviewer", id="cs1",
           name="complete_step",
           input={"status": "Success", "result": "DONE", "findings": findings}),
        ev("5", "tool_result", agent_id="step:spec/spec-reviewer", id="cs1",
           content="ok"),
        ev("6", "agent_output", agent_id="step:spec/spec-reviewer",
           status="Success", result="DONE", findings=findings),
        ev("7", "step_completed", step="spec-reviewer", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ]
    drv, out = _driver()
    for e in events:
        drv.on_event(e)
    drv.finish()
    body = _body(out)
    full = out.getvalue()
    # findings are hidden by default (E1) — "load-bearing" is in findings only.
    assert full.count("load-bearing") == 0
    # The narration is un-held and appears exactly once.
    assert full.count("No findings.") == 1
    # Live and log must be byte-identical (parity invariant).
    assert body == _render_body(events)


def test_interleaved_narration_does_not_permanently_hold_live_cursor() -> None:
    """Regression: a narration block followed by a thinking/tool turn and THEN a
    second narration must not stay `held` forever and freeze the live emit cursor.

    The opening narration of imp's first step ("I'll start by asking...") is held
    so a trailing narration can fold into the step conclusion. The un-hold used to
    only target `_last_narration_index`, which scans past trailing ToolBurstFold
    nodes ONLY — so a ThinkingFold / UserMessageNode between two narrations made it
    return None and the earlier narration was never un-held. A held node blocks the
    live walk, so the FIRST stuck-held narration froze the whole run at step one
    (no steps rendered beyond the opener until finish() force-flushed). Replays
    WITHOUT finish() to assert the real-time (mid-run) view, not the flushed one.
    """
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="spec-writer", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        # First narration — held.
        ev("3", "llm_response", agent_id="step:spec/spec-writer",
           raw={"content": [{"type": "text", "text": "First narration."}]}),
        # A thinking turn + a tool burst separate it from the next narration —
        # exactly what `_last_narration_index` cannot scan past.
        ev("4", "thinking", agent_id="step:spec/spec-writer", content="hmm"),
        ev("5", "tool_call", agent_id="step:spec/spec-writer", id="b1",
           name="grep", input={"q": "x"}),
        ev("6", "tool_result", agent_id="step:spec/spec-writer", id="b1",
           content="ok"),
        # Second narration — the new trailing block; must un-hold the first.
        ev("7", "llm_response", agent_id="step:spec/spec-writer",
           raw={"content": [{"type": "text", "text": "Second narration."}]}),
        ev("8", "tool_call", agent_id="step:spec/spec-writer", id="cs1",
           name="complete_step", input={"status": "Success"}),
        ev("9", "tool_result", agent_id="step:spec/spec-writer", id="cs1",
           content="ok"),
        ev("10", "agent_output", agent_id="step:spec/spec-writer", status="Success"),
        ev("11", "step_completed", step="spec-writer", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
        # A SECOND step. Before the fix its header never rendered live because the
        # held opening narration blocked the cursor.
        ev("12", "step_started", step="plan-writer", kind="agent", phase="plan",
           model="m", provider="anthropic"),
        ev("13", "llm_response", agent_id="step:plan/plan-writer",
           raw={"content": [{"type": "text", "text": "Planning."}]}),
    ]
    drv, out = _driver()
    for e in events:
        drv.on_event(e)
    # NO finish(): assert the LIVE, mid-run state.
    body = out.getvalue()
    assert "First narration." in body          # the opener un-held and emitted
    assert "spec-writer complete" in body       # step one concluded live
    assert "plan-writer" in body                # step two rendered live (not frozen)


def test_status_spinner_is_render_safe_ascii() -> None:
    from imp.cli.live_driver import _SPINNER, compose_activity_status
    from imp.cli.narrative import NarrativeBuilder
    from tests._event_fixtures import ev

    assert all(ord(c) < 128 for c in _SPINNER)   # no braille/tofu glyphs

    builder = NarrativeBuilder()
    builder.add(ev("1", "step_started", step="s", kind="agent", phase="p"))
    builder.add(ev("t1", "tool_call", agent_id="a", id="c1", name="bash",
                   input={"command": "ls"}))
    status = compose_activity_status(builder, frame=0, elapsed=1.0, color=False)
    assert status is not None
    assert status[0] == _SPINNER[0]              # the rendered spinner glyph is ASCII

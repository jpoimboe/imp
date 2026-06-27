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
        ev("4", "tool_call", agent_id="a", id="t1", name="sub_agent",
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
        ev("4", "tool_call", agent_id="a", id="t1", name="sub_agent",
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
    # A rename DOES emit a `• Set name to …` aside (tested in test_rename_line.py).
    drv, out = _driver()
    drv.on_event(ev("1", "change_created", title="add auth"))
    drv.on_event(ev("2", "change_renamed", id="ch1", old_title="add auth",
                    new_title="Add OAuth"))
    body = _body(out)
    assert not any(ln.startswith("#") for ln in body)


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
        ev("4", "tool_call", agent_id="a", id="t1", name="sub_agent",
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
        ev("4", "tool_call", agent_id="a", id="t1", name="sub_agent",
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
        ev("4", "tool_call", agent_id="a", id="t1", name="sub_agent",
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
        ev("4", "tool_call", agent_id="a", id="t1", name="sub_agent",
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


def test_background_subagent_row_keeps_duration_in_live() -> None:
    # Regression: a NON-BLOCKING subagent dispatch. The parent (opener "a") keeps
    # working — a concurrent read_file tool_call/result — WHILE the subagent runs,
    # and the subagent's authoritative `task` tool_result lands much later (10s).
    # The opener's concurrent activity must NOT resume-close the still-running fold
    # with a near-instant end_ts: doing so makes the live driver commit the row at
    # sub-1s elapsed (no duration), frozen, while `imp log` (which reads the final
    # end_ts) shows `· 10s`. Both views must stay byte-identical AND show the
    # duration.
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        # Block-tracked dispatch (id_ns="block"): an authoritative result WILL close
        # it, so the opener's concurrent activity must not resume-close it early.
        ev("4", "tool_call", agent_id="a", id="t1", name="sub_agent",
           input={"subagent_name": "reviewer"}, id_ns="block",
           timestamp="2026-01-01T00:00:00+00:00"),
        # Opener does concurrent work while the background subagent runs — this is
        # the event that used to premature-close the fold (not matched / not task /
        # not mid-turn → resume-close fired).
        ev("5", "tool_call", agent_id="a", id="r1", name="read_file",
           input={"p": "x"}, timestamp="2026-01-01T00:00:00.4+00:00"),
        ev("6", "tool_result", agent_id="a", id="r1", content="ok",
           timestamp="2026-01-01T00:00:00.5+00:00"),
        ev("7", "llm_response", agent_id="sub:4",
           raw={"content": [{"type": "text", "text": "the finding"}]}, usage={}),
        # Authoritative close, 10s after dispatch.
        ev("8", "tool_result", agent_id="a", id="t1", id_ns="block",
           content='{"status":"Success"}',
           timestamp="2026-01-01T00:00:10+00:00"),
        ev("9", "step_completed", step="spec", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ]
    drv, out = _driver()
    for e in events:
        drv.on_event(e)
    drv.finish()
    body = _body(out)
    assert body == _render_body(events)            # live == imp log, byte-for-byte
    reviewer_row = next(ln for ln in body if ln.strip().startswith("reviewer"))
    assert "10s" in reviewer_row, f"duration dropped from live row: {reviewer_row!r}"


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
        ev("4", "tool_call", agent_id="a", id="t1", name="sub_agent",
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
        ev("4", "tool_call", agent_id="a", id="t1", name="sub_agent",
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
        ev("4", "tool_call", agent_id="a", id="t1", name="sub_agent",
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
    assert "> do the thing" in out.getvalue()   # caret turn at column 0

    # TTY live: prompt_toolkit erased its echo, so the driver reprints the turn.
    tout = _TTYPipe()
    tdrv = LiveDriver(stream=tout, color=False, width=80, status=LiveStatus(tout))
    for e in events:
        tdrv.on_event(e)
    tdrv.finish()
    assert "> do the thing" in tout.getvalue()   # caret turn at column 0

    # imp log (no terminal echo) always renders the user turn.
    assert "> do the thing" in "\n".join(_render_body(events))


def test_final_open_phase_has_no_breadcrumb_in_live_or_log() -> None:
    """The final (never-closed) phase must NOT emit a <phase> complete breadcrumb in
    either the live path (finish()/force-pop) or the log (render_tree gated on
    node.closed). Uses distinct names for phase ("finalize") and step ("deploy")
    so a phase-level assert cannot accidentally match a step-level breadcrumb."""
    events = [
        {"type": "change_created", "agent_id": None, "payload": {"title": "t"},
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
        {"type": "change_created", "agent_id": None, "payload": {"title": "t"},
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
    live_line = next(ln for ln in out.getvalue().splitlines() if "spec phase complete" in ln)
    log_lines = render_tree(nv.build_tree(events), full=False, color=False, width=80)
    log_line = next(ln for ln in log_lines if "spec phase complete" in ln)
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
    bc_i = next(i for i, ln in enumerate(body) if "spec · complete" in ln)
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
        ev("4", "tool_call", agent_id="a", id="t1", name="sub_agent",
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
        ev("tk", "tool_call", agent_id="a", id="t1", name="sub_agent",
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


def test_tty_chrome_steps_no_committed_status_no_double_blanks(monkeypatch) -> None:
    """Regression (live-TTY): a chrome step's transient `· working` status line
    must NOT survive into scroll-back above its breadcrumb, and no two blank lines
    ever stack. Feed the byte stream through pyte (a faithful terminal) and assert
    the final screen: no `working` line committed, and no adjacent blank rows."""
    import os
    import shutil

    import pyte
    monkeypatch.setattr(shutil, "get_terminal_size",
                        lambda *a, **k: os.terminal_size((60, 30)))
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
    """The status block is transient: _refresh_status draws it (\\r-rewritten rows,
    no trailing newline) and must NOT append a separating "\\n" after the last row.
    A trailing newline commits the block into scroll-back and stacks with
    _print_lines' own separator into double blanks. Permanent spacing is
    _print_lines' job alone; `_prev_prose` is left untouched."""
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
    assert not written.endswith("\n"), f"status must not end with a newline; got {written!r}"
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


def test_status_span_restarts_on_prose_turns_steps_and_user_replies() -> None:
    """The status timer measures the agent's working burst since it last spoke.
    restart_span() fires on step_started, user_message, AND a genuine prose
    llm_response — but NOT on an empty-text (tool-use-carrier) llm_response."""
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
           model="m", provider="anthropic"),                              # restart
        ev("3", "llm_response", agent_id="a",
           raw={"content": [{"type": "tool_use", "id": "t", "name": "bash",
                             "input": {}}]}, usage={}),                    # NO restart (no prose)
        ev("4", "tool_call", agent_id="a", id="t", name="bash", input={}),
        ev("5", "tool_result", agent_id="a", id="t", content="ok"),
        ev("6", "llm_response", agent_id="a",
           raw={"content": [{"type": "text", "text": "ask"}]}, usage={}),  # restart (prose)
        ev("7", "user_message", content=[{"type": "text", "text": "reply"}]),  # restart
        ev("8", "llm_response", agent_id="a",
           raw={"content": [{"type": "text", "text": "ack"}]}, usage={}),  # restart (prose)
    ]:
        drv.on_event(e)
    assert calls == ["restart", "restart", "restart", "restart"]


def test_prose_does_not_restart_span_while_a_subagent_runs() -> None:
    """A parent prose llm_response restarts the span normally — but NOT while a
    dispatched subagent is still running, else the visible `(+N subagents) · Ns`
    elapsed zeroes for work that never stopped."""
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
           model="m", provider="anthropic"),                              # restart
        ev("3", "tool_call", agent_id="a", id="t1", name="sub_agent",
           input={"subagent_name": "reviewer"}),                         # fold runs
        ev("4", "llm_response", agent_id="a",
           raw={"content": [{"type": "text", "text": "still working"}]},
           usage={}),                                                    # NO restart
    ]:
        drv.on_event(e)
    assert calls == ["restart"]                                          # only step_started


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
    assert any("gate · complete" in ln for ln in body)                  # breadcrumb
    assert body == _render_body(events)                                 # live == imp log


def test_prepare_for_prompt_flushes_separator_with_indent_bytes() -> None:
    drv, out = _driver()
    drv.on_event(ev("2", "step_started", step="s", kind="agent", phase="design",
                    model="m", provider="anthropic"))
    # step header emitted, _prev_prose armed for the step body
    drv.prepare_for_prompt()
    # the armed separator is a bare blank line (depth 0 = column 0, no leading spaces)
    assert out.getvalue().endswith("\n\n")
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
    assert "loading skill refactor" in out.getvalue()   # flushed at prompt exit


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


def test_quiet_forward_goto_renders_nothing_matches_log() -> None:
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
    assert not any("fast-path-spec-gate" in ln for ln in body)
    assert not any(ln.strip().startswith("→") for ln in body)
    assert body == _render_body(events)   # live == imp log, line for line


def test_is_final_closed_quiet_goto_step_is_emitted() -> None:
    # A closed quiet step (here a backward rework goto resume=True) is FINAL
    # immediately — the live cursor must not block on it.
    drv, _ = _driver()
    node = nv.StepNode(name="spec-verify-commit", kind="script", quiet=True)
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
    assert "thinking" in line           # verb head for unlabeled agent step
    assert "1 call" in line
    assert "bash ls" in line
    assert "\x1b[90m" in line          # detail painted gray
    assert "|" not in line             # no old ASCII spinner


def test_compose_status_is_flush_left_at_any_frontier_depth() -> None:
    """The transient status line renders flush-left — no gutter indent — even
    while a step is streaming at a deeper frontier."""
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    drv.on_event(ev("1", "step_started", step="s", kind="agent", phase="p"))
    drv.on_event(ev("t1", "tool_call", agent_id="a", id="c1", name="bash",
                    input={"command": "ls"}))
    assert drv._frontier_depth() == 0                  # streaming flush at column 0
    result = drv.compose_status(0, 1.0)
    assert isinstance(result, list) and len(result) == 3
    assert not result[0].startswith(" ")               # spinner at column 0
    assert result[0][0] == "⠋"
    # position bar is on result[-1]
    assert not result[-1].startswith(" ")



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


def test_loud_forward_goto_breadcrumb_emits_immediately() -> None:
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
    # The closed step's breadcrumb emits at step_completed (not held): the gray
    # completion line is present before plan-writer's step_started arrives.
    for e in events[:4]:
        drv.on_event(e)
    assert any("spec-reviewer · complete" in ln for ln in _body(out))
    for e in events[4:]:
        drv.on_event(e)
    drv.finish()
    body = _body(out)
    assert not any(ln.strip().startswith("→") for ln in body)   # unlabeled goto: silent
    assert not any("goto(" in ln for ln in body)
    assert body == _render_body(events)   # live == imp log, line for line


def test_is_final_goto_breadcrumb_is_emitted() -> None:
    # A loud reviewer rework (goto + rework) breadcrumb is final immediately — the
    # route label rides it inline at completion, so the live cursor never holds it.
    drv, _ = _driver()
    bc = nv.BreadcrumbLine(step_name="plan-review", elapsed_s=1.0,
                           action={"verb": "goto", "target": "plan-writer",
                                   "resume": False, "rework": True})
    assert drv._is_final(bc, container=None) is True


def test_change_merged_suppressed_in_live_path() -> None:
    drv, out = _driver()
    for e in [
        ev("0", "change_created", id="run-x",
           title="Run X"),
        ev("1", "step_started", step="merge-to-base",
           kind="merge_and_exit_worktree", phase="review"),
        ev("2", "step_completed", step="merge-to-base", kind="merge_and_exit_worktree",
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
           name="sub_agent", input={"subagent_name": f"member-{eid}",
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
    assert "spec-writer · complete" in body       # step one concluded live
    assert "plan-writer" in body                # step two rendered live (not frozen)


def test_status_spinner_uses_braille_glyph() -> None:
    from imp.cli.live_driver import compose_activity_status
    from imp.cli.narrative import NarrativeBuilder

    builder = NarrativeBuilder()
    builder.add(ev("1", "step_started", step="s", kind="agent", phase="p"))
    builder.add(ev("t1", "tool_call", agent_id="a", id="c1", name="bash",
                   input={"command": "ls"}))
    status = compose_activity_status(builder, frame=0, elapsed=1.0, color=False)
    assert status is not None
    assert status[0] == "⠋"          # braille frame 0 is the first character
    assert "|" not in status          # the "|/-\\" cycle is gone
    assert "thinking" in status       # verb head for unlabeled agent step
    assert "1 call" in status         # burst count in detail
    assert "bash ls" in status        # in-flight tool in detail


def test_status_line_shows_step_then_activity_then_elapsed() -> None:
    from imp.cli.live_driver import compose_activity_status

    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="plan-review", kind="agent", phase="review",
           model="m", provider="anthropic", label="reviewing plan"),
        ev("3", "thinking", agent_id="step:review/plan-review", content="hmm"),
    ]:
        b.add(e)
    line = compose_activity_status(b, frame=0, elapsed=18.0, color=False)
    assert "reviewing plan · 18s" in line
    assert "thinking" not in line                  # the thinking verb is dropped
    assert "review · reviewing plan" not in line   # the phase is NOT shown in the status line


def test_labeled_route_reason_rides_target_header_in_both_paths() -> None:
    drv, out = _driver()
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="code-reviewer", kind="agent", phase="review",
           model="m", provider="anthropic"),
        ev("3", "llm_response", agent_id="step:review/code-reviewer",
           raw={"content": [{"type": "text", "text": "rework"}]}, usage={}),
        ev("4", "step_completed", step="code-reviewer", kind="agent",
           action={"verb": "goto", "target": "task-lead",
                   "resume": False, "rework": True},
           var_mutations={}, label="addressing review feedback"),
        ev("5", "step_started", step="task-lead", kind="agent", phase="implement",
           model="opus", provider="anthropic",
           route_label="addressing review feedback"),
    ]
    for e in events:
        drv.on_event(e)
    drv.finish()
    body = _body(out)
    # No "→ <label>" route line beneath the completion any more.
    assert not any(ln.strip().startswith("→ addressing review feedback") for ln in body)
    # The reason rides the task-lead header as the last suffix segment.
    hdr = next(ln for ln in body if "task-lead" in ln and "complete" not in ln)
    assert hdr.strip().endswith("· addressing review feedback")
    # Live path and the lean render_text path stay byte-identical.
    assert body == _render_body(events)


def test_held_narration_emits_before_open_burst_live() -> None:
    # A trailing narration is created held=True so it can fold into a step
    # conclusion. With eager emission it must still emit immediately; only the
    # OPEN tool burst that follows it may be withheld. Replays WITHOUT finish() —
    # the real, mid-run state a watching human sees.
    drv, out = _driver()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("3", "llm_response", agent_id="a",
           raw={"content": [{"type": "text", "text": "Reading the files now."}]},
           usage={}),
        ev("4", "tool_call", agent_id="a", id="b1", name="read_file",
           input={"p": "x"}),
    ]:
        drv.on_event(e)
    body = out.getvalue()
    assert "Reading the files now." in body          # held narration emits live
    assert "read_file" not in body  # open burst still withheld


def test_quiet_goto_renders_nothing_in_live_path() -> None:
    drv, out = _driver()
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="spec-reviewer", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        ev("3", "llm_response", agent_id="step:spec/spec-reviewer",
           raw={"content": [{"type": "text", "text": "approved"}]}, usage={}),
        ev("4", "step_completed", step="spec-reviewer", kind="agent",
           action={"verb": "goto", "target": "plan-writer",
                   "resume": False, "rework": False},
           var_mutations={}, label=None),
        ev("5", "step_started", step="plan-writer", kind="agent", phase="plan",
           model="m", provider="anthropic"),
        ev("6", "step_completed", step="plan-writer", kind="agent",
           action={"verb": "continue"}, var_mutations={}, label=None),
    ]
    for e in events:
        drv.on_event(e)
    drv.finish()
    body = _body(out)
    assert any("spec-reviewer · complete" in ln for ln in body)
    assert not any(ln.strip().startswith("→") for ln in body)
    assert not any("goto(" in ln for ln in body)
    assert body == _render_body(events)   # live == imp log, line for line


def test_current_activity_detail_anchors_to_in_flight_call() -> None:
    # Detail names the running call (c2), not a resolved (c1) or killed (c3) one.
    b = nv.NarrativeBuilder()
    b.add(ev("1", "step_started", step="s", kind="agent", phase="p", label="searching"))
    b.add(ev("t1", "tool_call", agent_id="a", id="c1", name="read_file",
            input={"p": "x"}))
    b.add(ev("r1", "tool_result", agent_id="a", id="c1", content="ok"))
    b.add(ev("t2", "tool_call", agent_id="a", id="c2", name="bash",
            input={"command": "ls"}))           # still running
    b.add(ev("t3", "tool_call", agent_id="a", id="c3", name="grep",
            input={"q": "y"}))
    b.add(ev("k3", "tool_killed", agent_id="a", id="c3", name="grep"))
    act = b.current_activity()
    assert act is not None
    assert act.verb == "searching"
    assert "bash ls" in (act.detail or "")   # in-flight call (c2), not killed c3
    assert "3 calls" in (act.detail or "")   # count basis keeps killed


def test_compose_activity_status_has_no_per_call_timer() -> None:
    from imp.cli.live_driver import compose_activity_status

    b = nv.NarrativeBuilder()
    b.add(ev("1", "step_started", step="implement", kind="agent", phase="impl",
             label="implementing"))
    b.add(ev("t1", "tool_call", agent_id="a", id="c1", name="bash",
            input={"command": "sleep 600"}))
    line = compose_activity_status(b, frame=0, elapsed=900.0, color=False)
    assert line is not None
    # Only the single trailing burst timer (900s = 15m 0s); no per-call clock.
    assert line.split(" ", 1)[1] == "implementing · 1 call · bash sleep 600 · 15m 0s"


def test_status_spine_label_activity_elapsed() -> None:
    from imp.cli.live_driver import compose_activity_status

    # labeled agent step: verb · elapsed in the activity line (no phase, no thinking).
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="plan-reviewer", kind="agent", phase="plan",
           model="m", provider="anthropic", label="reviewing plan"),
        ev("3", "thinking", agent_id="step:plan/plan-reviewer", content="hmm"),
    ]:
        b.add(e)
    line = compose_activity_status(b, frame=0, elapsed=18.0, color=False, width=80)
    # Activity line: spinner · verb · elapsed. Identity (title/model) is in the
    # position bar — not in the activity line.
    assert line.startswith("⠋ reviewing plan · 18s")
    suffix = line[len("⠋ reviewing plan · 18s"):]
    assert "x" in suffix   # title appears in position bar (later in combined line)
    assert "m" in suffix   # model appears in position bar (later in combined line)

    # quiet/unlabeled script step: no display label, no command → bare timer.
    q = nv.NarrativeBuilder()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="autosquash", kind="script", phase="implement",
           quiet=True),
    ]:
        q.add(e)
    qline = compose_activity_status(q, frame=0, elapsed=4.0, color=False, width=80)
    assert qline.startswith("⠋ 4s")     # bare timer leads (no verb, no detail)
    assert "x" in qline                 # title appears in position bar (joined into combined line)


def test_live_parallel_batch_rows_show_correct_elapsed() -> None:
    # Tripwire: a streaming chunk must not resume-close sibling folds, freezing their elapsed.
    drv, out = _driver()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("tk1", "tool_call", agent_id="a", id="b1", name="sub_agent",
           input={"subagent_name": "reviewer"},
           timestamp="2026-06-19T10:00:00+00:00"),
        ev("tk2", "tool_call", agent_id="a", id="b2", name="sub_agent",
           input={"subagent_name": "tester"},
           timestamp="2026-06-19T10:00:01+00:00"),
        ev("a1", "llm_response", agent_id="a", stop_reason=None, usage={},
           raw={"content": [{"type": "text", "text": "still working"}]},
           timestamp="2026-06-19T10:00:02+00:00"),
        ev("r1", "tool_result", agent_id="a", id="b1", id_ns="block",
           content="done", timestamp="2026-06-19T10:00:10+00:00"),
        ev("r2", "tool_result", agent_id="a", id="b2", id_ns="block",
           content="done", timestamp="2026-06-19T10:00:20+00:00"),
        ev("sc", "step_completed", step="spec", kind="agent",
           action={"verb": "continue"}, var_mutations={},
           timestamp="2026-06-19T10:00:21+00:00"),
    ]:
        drv.on_event(e)
    drv.finish()
    body = _body(out)
    assert any("reviewer" in ln and "10s" in ln for ln in body)
    assert any("tester" in ln and "19s" in ln for ln in body)


def test_prompt_indent_matches_open_step_frontier_depth() -> None:
    drv, _out = _driver()
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("3", "thinking", agent_id="step:design/spec", content="hmm"),
    ]
    for e in events:
        drv.on_event(e)
    # A step under a phase renders its content at column 0 (the transcript is flat),
    # so the solicit caret has no indent — the same column its echoed reply renders at.
    assert drv.prompt_indent() == ""


def test_prompt_indent_empty_when_no_open_step() -> None:
    drv, _out = _driver()
    drv.on_event(ev("1", "change_created", change_id=1, title="x"))
    assert drv.prompt_indent() == ""


def test_multiphase_subagent_reentry_live_log_parity() -> None:
    # A multi-phase run with a subagent and an implement-loop re-entry: the live body
    # must be byte-equal to the imp log body, the re-entry must print a second
    # `── implement phase ───` start rule, and a closed phase's end rule must land at column 0.
    events = [
        ev("e0", "change_created", change_id=1, title="t", timestamp="2026-06-03T00:00:00+00:00"),
        ev("e1", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic", timestamp="2026-06-03T00:00:00+00:00"),
        ev("e2", "step_completed", step="spec", kind="agent",
           action={"verb": "continue"}, var_mutations={}, timestamp="2026-06-03T00:00:01+00:00"),
        ev("e3", "step_started", step="build", kind="agent", phase="implement",
           model="m", provider="anthropic", timestamp="2026-06-03T00:00:01+00:00"),
        ev("e4", "tool_call", agent_id="a", id="t1", name="sub_agent",
           input={"subagent_name": "reviewer"}),
        ev("e5", "llm_response", agent_id="sub:e4",
           raw={"content": [{"type": "text", "text": "finding"}]}, usage={}),
        ev("e6", "tool_result", agent_id="a", id="t1", content='{"status":"Success"}'),
        ev("e7", "step_completed", step="build", kind="agent",
           action={"verb": "continue"}, var_mutations={}, timestamp="2026-06-03T00:00:02+00:00"),
        ev("e8", "step_started", step="replan", kind="agent", phase="plan",
           model="m", provider="anthropic", timestamp="2026-06-03T00:00:02+00:00"),
        ev("e9", "step_completed", step="replan", kind="agent",
           action={"verb": "continue"}, var_mutations={}, timestamp="2026-06-03T00:00:03+00:00"),
        ev("e10", "step_started", step="build", kind="agent", phase="implement",
           model="m", provider="anthropic", timestamp="2026-06-03T00:00:03+00:00"),
        ev("e11", "step_completed", step="build", kind="agent",
           action={"verb": "continue"}, var_mutations={}, timestamp="2026-06-03T00:00:04+00:00"),
    ]
    drv, out = _driver()
    for e in events:
        drv.on_event(e)
    drv.finish()
    body = _body(out)
    assert body == _render_body(events)                       # live == imp log, byte-for-byte
    assert sum(1 for ln in body if ln.strip() == "── implement phase ───") == 2  # re-entry rule
    end_rule = next(ln for ln in body if "implement phase complete" in ln)
    assert end_rule.startswith("implement phase complete")  # gray end line flush at column 0

def _two_phase_events():
    return [
        {"type": "change_created", "agent_id": None, "payload": {"title": "t"},
         "id": "e0", "timestamp": "2026-06-03T00:00:00+00:00"},
        {"type": "step_started", "agent_id": None,
         "payload": {"step": "draft", "kind": "branch", "phase": "spec"},
         "id": "e1", "timestamp": "2026-06-03T00:00:00+00:00"},
        {"type": "step_completed", "agent_id": None,
         "payload": {"step": "draft", "kind": "branch", "action": {"verb": "continue"}},
         "id": "e2", "timestamp": "2026-06-03T00:00:05+00:00"},
        {"type": "step_started", "agent_id": None,
         "payload": {"step": "refine", "kind": "branch", "phase": "plan"},
         "id": "e3", "timestamp": "2026-06-03T00:00:05+00:00"},
        {"type": "step_completed", "agent_id": None,
         "payload": {"step": "refine", "kind": "branch", "action": {"verb": "continue"}},
         "id": "e4", "timestamp": "2026-06-03T00:00:09+00:00"},
    ]


def _opening_intent_events():
    # opening_intent is emitted BEFORE the first step_started, so its
    # UserMessageNode lands at the workflow root, before the first phase header.
    return [
        {"type": "change_created", "agent_id": None, "payload": {"title": "t"},
         "id": "e0", "timestamp": "2026-06-03T00:00:00+00:00"},
        {"type": "opening_intent", "agent_id": None,
         "payload": {"text": "build the parser"},
         "id": "e1", "timestamp": "2026-06-03T00:00:00+00:00"},
        {"type": "step_started", "agent_id": None,
         "payload": {"step": "draft", "kind": "branch", "phase": "spec"},
         "id": "e2", "timestamp": "2026-06-03T00:00:00+00:00"},
        {"type": "step_completed", "agent_id": None,
         "payload": {"step": "draft", "kind": "branch", "action": {"verb": "continue"}},
         "id": "e3", "timestamp": "2026-06-03T00:00:05+00:00"},
    ]


def test_opening_intent_does_not_drop_following_step_output() -> None:
    # Regression (change fbb6r4): the opening_intent UserMessageNode is appended at
    # the workflow root; the live cursor emits it and advances PAST its slot. The
    # next step_started then re-homes that node under the step (_inject_opening_intent)
    # and inserts the PhaseNode at the index the cursor has ALREADY passed, so the
    # whole step subtree — phase header, step header, and every llm_response — slips
    # behind the monotonic cursor and is never emitted. The live view sat blank while
    # imp log (full re-walk) showed all the activity. BOTH the opening prompt AND the
    # agent's output must render live.
    drv, out = _driver()
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("e1", "opening_intent", text="build the parser"),
        ev("3", "step_started", step="spec-writer", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        ev("8", "llm_response", agent_id="a",
           raw={"content": [{"type": "text", "text": "drafting the spec now"}]},
           usage={}),
        ev("9", "step_completed", step="spec-writer", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ]
    for e in events:
        drv.on_event(e)
    drv.finish()
    body = _body(out)
    assert any("build the parser" in ln for ln in body)      # opening prompt
    assert any("drafting the spec now" in ln for ln in body)  # step output (was dropped)
    # The live body must match imp log's full re-walk exactly (shared render path).
    assert body == _render_body(events)


def test_suppress_replay_hides_marked_prefix_and_renders_live_tail() -> None:
    events = _two_phase_events()
    # The spec phase (first three events) is the replayed prefix; the plan phase
    # is the live continuation.
    marked = [{**e, "_replay": True} for e in events[:3]] + events[3:]
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80,
                     status=LiveStatus(out), suppress_replay=True)
    for e in marked:
        drv.on_event(e)
    drv.finish()
    body = _body(out)
    assert not any("spec" in ln or "draft" in ln for ln in body)
    assert any(ln == "── plan phase ───" for ln in body)
    assert any("refine" in ln for ln in body)


def test_suppress_replay_advances_cursor_no_redump() -> None:
    events = _two_phase_events()
    marked = [{**e, "_replay": True} for e in events[:3]] + events[3:]
    # Reference: no suppression renders every event (markers ignored).
    fout = _Pipe()
    fdrv = LiveDriver(stream=fout, color=False, width=80,
                      status=LiveStatus(fout), suppress_replay=False)
    for e in marked:
        fdrv.on_event(e)
    fdrv.finish()
    full = [ln for ln in _body(fout) if ln]
    # Suppressed resume: prefix hidden, tail drawn once.
    sout = _Pipe()
    sdrv = LiveDriver(stream=sout, color=False, width=80,
                      status=LiveStatus(sout), suppress_replay=True)
    for e in marked:
        sdrv.on_event(e)
    sdrv.finish()
    supp = [ln for ln in _body(sout) if ln]
    assert all("spec" not in ln and "draft" not in ln for ln in supp)
    anchor = full.index("── plan phase ───")
    assert supp == full[anchor:]


def test_no_suppression_renders_full_history() -> None:
    events = _two_phase_events()
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80,
                     status=LiveStatus(out), suppress_replay=False)
    for e in events:
        drv.on_event(e)
    drv.finish()
    assert _body(out) == _render_body(events)


def test_suppress_replay_hides_replayed_opening_intent() -> None:
    replayed = [{**e, "_replay": True} for e in _opening_intent_events()]
    live = [{
        "type": "step_started", "agent_id": None,
        "payload": {"step": "refine", "kind": "branch", "phase": "plan"},
        "id": "e9", "timestamp": "2026-06-03T00:00:09+00:00",
    }]
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80,
                     status=LiveStatus(out), suppress_replay=True)
    for e in replayed + live:
        drv.on_event(e)
    drv.finish()
    body = _body(out)
    assert not any("build the parser" in ln for ln in body)
    assert any(ln == "── plan phase ───" for ln in body)


def test_suppress_replay_with_no_replay_prefix_renders_full_history() -> None:
    events = _two_phase_events()
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80,
                     status=LiveStatus(out), suppress_replay=True)
    for e in events:
        drv.on_event(e)
    drv.finish()
    assert _body(out) == _render_body(events)


def test_suppress_replay_pure_replay_renders_nothing() -> None:
    events = _two_phase_events()
    marked = [{**e, "_replay": True} for e in events]
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80,
                     status=LiveStatus(out), suppress_replay=True)
    for e in marked:
        drv.on_event(e)
    drv.finish()
    assert all(not ln for ln in _body(out))


def test_resume_with_preceding_blank_omits_driver_leading_separator() -> None:
    # Interactive resume: the rendered splash already emitted the single pre-
    # first-emission blank (preceded_by_blank=True). The first LIVE node after
    # the replayed prefix is a container (the plan phase header — the agent
    # resuming work), so the driver must NOT prepend its own separator on top of
    # the splash's blank. The first emitted line is the phase header, no blank
    # above it.
    events = _two_phase_events()
    marked = [{**e, "_replay": True} for e in events[:3]] + events[3:]
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out),
                     suppress_replay=True, preceded_by_blank=True)
    for e in marked:
        drv.on_event(e)
    drv.finish()
    assert out.getvalue().split("\n")[0] == "── plan phase ───"


def test_resume_without_preceding_blank_keeps_driver_leading_separator() -> None:
    # Headless / non-interactive resume: no splash renders, so only a run-header
    # line (with no trailing blank) precedes the driver (preceded_by_blank=False,
    # the default). The driver MUST keep its own separator before the first
    # container emission so the phase header does not butt against the run-header
    # — matching the from-scratch non-resume rendering. The first emitted line is
    # the blank separator.
    events = _two_phase_events()
    marked = [{**e, "_replay": True} for e in events[:3]] + events[3:]
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out),
                     suppress_replay=True, preceded_by_blank=False)
    for e in marked:
        drv.on_event(e)
    drv.finish()
    assert out.getvalue().split("\n")[0] == ""
    assert out.getvalue().split("\n")[1] == "── plan phase ───"


def test_resume_user_first_message_has_no_driver_leading_separator() -> None:
    # User-first resume: the first LIVE node is a user message (a leaf, never a
    # container). It already shows no driver leading separator today; this guards
    # that the preceded_by_blank suppression leaves that flow unchanged — the
    # message is the first emitted line, no blank above it.
    events = [
        ev("1", "change_created", change_id=1, title="t"),
        ev("2", "step_started", step="spec", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        ev("2b", "awaiting_user_input", agent_id="step:spec/spec"),
        ev("3", "user_message", content=[{"type": "text", "text": "do the thing"}]),
    ]
    marked = [{**e, "_replay": True} for e in events[:3]] + events[3:]
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out),
                     suppress_replay=True, preceded_by_blank=True)
    for e in marked:
        drv.on_event(e)
    drv.finish()
    assert out.getvalue().split("\n")[0] == "> do the thing"

    # Drive _prev_prose=True so the assertion actually falsifies the _emitted_any
    # guard: without the guard a spurious blank would land here.
    out2 = _Pipe()
    drv2 = LiveDriver(stream=out2, color=False, width=80, status=LiveStatus(out2),
                      preceded_by_blank=True)
    drv2._prev_prose = True
    drv2._builder._awaiting_reply = True
    drv2.on_event(ev("1", "user_message", content=[{"type": "text", "text": "do the thing"}]))
    drv2.finish()
    assert out2.getvalue().split("\n")[0] == "> do the thing"


def test_compose_activity_status_spinner_uses_wordmark_not_cyan() -> None:
    from imp.cli import palette as pal
    from imp.cli.live_driver import compose_activity_status
    try:
        for theme in ("dark", "light", "ansi"):
            pal.set_theme(theme)
            b = nv.NarrativeBuilder()
            b.add(ev("1", "step_started", step="s", kind="agent", phase="p"))
            line = compose_activity_status(b, frame=0, elapsed=1.0, color=True)
            assert line is not None
            # The activity line uses the wordmark color for the spinner — never cyan.
            # No background prefix on the activity line (background is on the position bar).
            assert pal._ANSI["wordmark"] in line             # wordmark spinner present
            assert pal._ANSI["cyan"] not in line             # no longer cyan
            bg = pal.user_bg_open(enabled=True)
            if bg:  # only check when the theme has a bg color
                assert not line.startswith(bg)               # no bg prefix on activity line
    finally:
        pal.set_theme("ansi")


def test_context_bar_renders_filled_track_and_percent_plain() -> None:
    from imp.cli.live_driver import context_bar
    bar = context_bar("sonnet", 26000, color=False, over_bg=False)
    assert bar == "█░░░░░░░░░ 13%"        # 26000/200000 = 0.13 -> 1 of 10 filled


def test_context_bar_color_thresholds_green_yellow_red() -> None:
    from imp.cli.live_driver import context_bar
    green = context_bar("sonnet", 100000, color=True, over_bg=False)   # 50% < 60
    yellow = context_bar("sonnet", 140000, color=True, over_bg=False)  # 70% < 85
    red = context_bar("sonnet", 180000, color=True, over_bg=False)     # 90% >= 85
    assert "\x1b[32m" in green                                          # green fill
    assert "\x1b[33m" in yellow                                         # yellow fill
    assert "\x1b[31m" in red                                            # red fill
    # boundary checks: exactly 60% is yellow (not green), exactly 85% is red (not yellow)
    boundary_yellow = context_bar("sonnet", 120000, color=True, over_bg=False)
    boundary_red = context_bar("sonnet", 170000, color=True, over_bg=False)
    assert "\x1b[33m" in boundary_yellow and "\x1b[32m" not in boundary_yellow
    assert "\x1b[31m" in boundary_red and "\x1b[33m" not in boundary_red


def test_context_bar_midrange_fill_is_wordmark_under_dark_theme() -> None:
    from imp.cli import palette as pal
    from imp.cli.live_driver import context_bar
    try:
        pal.set_theme("dark")
        bar = context_bar("sonnet", 140000, color=True, over_bg=False)  # 70% -> mid-range
        assert pal.sgr("wordmark", enabled=True) in bar   # \x1b[38;2;255;154;60m
        assert pal.sgr("yellow", enabled=True) not in bar  # not the muted yellow role
    finally:
        pal.set_theme("ansi")


def test_context_bar_falls_back_to_catalog_window() -> None:
    from imp.cli.live_driver import context_bar
    from imp.providers.catalog import window_for_model
    assert window_for_model("claude-sonnet-4-5") == 200_000
    assert window_for_model("opus") == 200_000
    assert window_for_model("opus-1m") == 1_000_000
    assert window_for_model("gpt-5.5") == 400_000
    # 40000/200000 = 0.2 -> round(0.2*10) = 2 filled cells, 8 track.
    assert context_bar("claude-opus-4-1", 40000, color=False, over_bg=False) == \
        "██░░░░░░░░ 20%"


def test_context_bar_prefers_sdk_percentage_over_tokens() -> None:
    from imp.cli.live_driver import context_bar
    # context_pct is rendered directly: a huge prompt_tokens that would imply
    # ~100% is ignored when the SDK percentage is present.
    bar = context_bar("opus-1m", 999_999, context_pct=42.0,
                      color=False, over_bg=False)
    # 42% -> round(0.42*10) = 4 filled cells, 6 track.
    assert bar == "████░░░░░░ 42%"


def test_live_driver_has_no_context_windows_symbol() -> None:
    import imp.cli.live_driver as ld
    assert not hasattr(ld, "_CONTEXT_WINDOWS")
    assert not hasattr(ld, "context_window")


def test_context_bar_omitted_for_unknown_model_or_missing_usage() -> None:
    from imp.cli.live_driver import context_bar
    assert context_bar("some-unlisted-model", 1000, color=False, over_bg=False) is None
    assert context_bar(None, 1000, color=False, over_bg=False) is None
    assert context_bar("sonnet", None, color=False, over_bg=False) is None


def test_compose_position_bar_includes_change_id_title_position_model() -> None:
    import imp.cli.narrative as nv_mod
    from imp.cli.live_driver import compose_position_bar
    builder = nv_mod.NarrativeBuilder()
    builder.add(ev("1", "change_created", id="z9gixx", title="streaming toolbar"))
    builder.add(ev("2", "step_started", step="s", kind="agent", phase="p",
                   model="sonnet", phase_label="implement", label="task-lead"))
    activity = builder.current_activity()
    root = builder.tree.root
    bar = compose_position_bar(activity=activity, change_id=root.id, title=root.title,
                               prompt_tokens=None, color=False, width=80)
    # Identity (id, title) and breadcrumb (position=phase_label, model) all appear.
    assert "z9gixx" in bar and "streaming toolbar" in bar
    assert "implement" in bar    # position = phase_label for non-position_label step
    assert "sonnet" in bar       # model appears in position bar


def test_compose_toolbar_static_uses_frame_zero_and_status_elapsed() -> None:
    out = _Pipe()
    status = LiveStatus(out, now=lambda: 7.0)
    drv = LiveDriver(stream=out, color=False, width=80, status=status)
    drv.on_event(ev("1", "change_created", change_id=1, title="t"))
    drv.on_event(ev("2", "step_started", step="s", kind="agent", phase="p",
                    model="sonnet", phase_label="impl phase"))
    static = drv.compose_toolbar_static()
    assert static is not None
    assert "⠋" not in static          # position bar has no spinner (AC5)
    assert "0s" not in static          # position bar has no elapsed (AC5)
    assert "sonnet" in static          # model appears in position bar
    assert "impl phase" in static      # position breadcrumb present


def test_compose_toolbar_static_none_when_idle() -> None:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    assert drv.compose_toolbar_static() is None    # no activity -> no toolbar


def test_live_driver_caps_hr_at_display_width() -> None:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=10000, display_width=40,
                     status=LiveStatus(out))
    long_line = "X" * 200
    drv.on_event(ev("1", "change_created", change_id=1, title="x"))
    drv.on_event(ev("2", "step_started", step="s", kind="agent", phase="p",
                    model="m", provider="anthropic"))
    drv.on_event(ev("3", "llm_response", agent_id="a", usage={},
                    raw={"content": [{"type": "text",
                                      "text": long_line + "\n\n---\n\nafter"}]}))
    drv.on_event(ev("4", "step_completed", step="s", kind="agent",
                    action={"verb": "continue"}, var_mutations={}))
    body = _body(out)
    assert any(long_line in ln for ln in body)        # prose unwrapped (wrap width)
    assert any(ln.count("─") == 40 for ln in body)    # HR capped to the display width
    assert not any(ln.count("─") > 40 for ln in body)


def test_compose_position_bar_is_flush_left_with_position_and_bar() -> None:
    from imp.cli.live_driver import compose_position_bar
    act = nv.Activity(position="implementation phase · plan 1/1 · task 2/3",
                      model="claude-sonnet-4-5")
    line = compose_position_bar(activity=act, change_id="z9gixx", title="Fix bug",
                                prompt_tokens=1000, color=False, width=120)
    assert line.lstrip().startswith("z9gixx")
    assert "implementation phase · plan 1/1 · task 2/3" in line
    assert "Fix bug" in line


def test_compose_position_bar_never_contains_the_verb() -> None:
    from imp.cli.live_driver import compose_position_bar
    act = nv.Activity(position="planning phase · plan 2/4", verb="writing plan",
                      detail="3 calls", model="claude-sonnet-4-5")
    line = compose_position_bar(activity=act, change_id="z9gixx", title="t",
                                prompt_tokens=1000, color=False, width=120)
    assert "writing plan" not in line
    assert "3 calls" not in line
    assert "planning phase · plan 2/4" in line


def test_compose_activity_line_is_verb_detail_elapsed_with_spinner() -> None:
    from imp.cli.live_driver import compose_activity_line
    act = nv.Activity(verb="coding", detail="bash pytest", model="m")
    line = compose_activity_line(activity=act, frame=0, elapsed=14.0, color=False)
    assert line is not None
    assert "coding" in line and "bash pytest" in line and "14s" in line


def test_compose_activity_line_animates_spinner_and_elapsed_when_verbless() -> None:
    from imp.cli.live_driver import compose_activity_line
    act = nv.Activity(position="implementation phase · plan 1/1", verb=None, detail=None)
    line = compose_activity_line(activity=act, frame=0, elapsed=4.0, color=False)
    assert line == "⠋ 4s"


def test_compose_position_bar_slate_survives_livestatus_clip_under_dark_theme() -> None:
    from imp.cli import palette as pal
    from imp.cli.live_driver import compose_position_bar
    act = nv.Activity(position="planning phase · plan 1/1", model="claude-sonnet-4-5")
    try:
        pal.set_theme("dark")
        line = compose_position_bar(activity=act, change_id="z9gixx", title="t",
                                    prompt_tokens=1000, color=True, width=120)
    finally:
        pal.set_theme("ansi")
    assert line.startswith("\x1b[48;2;43;47;58m")
    assert line.endswith("\x1b[0m")
    assert pal.display_width(line) == 118
    clipped = pal.clip_display(line, 120 - 1)
    assert clipped == line
    assert "…" not in clipped


def test_compose_activity_status_combined_line_does_not_overflow_under_slate() -> None:
    from imp.cli import palette as pal
    from imp.cli.live_driver import compose_activity_status
    b = nv.NarrativeBuilder()
    b.add(ev("1", "change_created", change_id=1, title="t"))
    b.add(ev("2", "step_started", step="task-lead", kind="agent", phase="code",
             model="claude-sonnet-4-5", provider="anthropic", label="task 1/3",
             phase_label="implementation phase · plan 1/1", position_label=True))
    try:
        pal.set_theme("dark")
        line = compose_activity_status(b, frame=0, elapsed=5.0, color=True, width=120)
    finally:
        pal.set_theme("ansi")
    assert line is not None
    assert pal.display_width(line) <= 120 - 1
    assert pal.clip_display(line, 120 - 1) == line


def test_compose_status_returns_activity_line_blank_then_position_bar() -> None:
    drv, _ = _driver()
    drv.on_event(ev("1", "change_created", change_id=1, title="t"))
    drv.on_event(ev("2", "step_started", step="task-lead", kind="agent", phase="code",
                     model="claude-sonnet-4-5", provider="anthropic", label="task 1/3",
                     phase_label="implementation phase · plan 1/1", position_label=True))
    block = drv.compose_status(0, 5.0)
    assert isinstance(block, list) and len(block) == 3
    assert "thinking" in block[0]
    assert block[1] == ""
    assert "implementation phase · plan 1/1 · task 1/3" in block[2]
    assert "thinking" not in block[2]


def test_compose_toolbar_static_is_position_bar_only() -> None:
    drv, _ = _driver()
    drv.on_event(ev("1", "change_created", change_id=1, title="t"))
    drv.on_event(ev("2", "step_started", step="task-lead", kind="agent", phase="code",
                     model="claude-sonnet-4-5", provider="anthropic", label="task 1/3",
                     phase_label="implementation phase · plan 1/1", position_label=True))
    toolbar = drv.compose_toolbar_static()
    assert toolbar is not None
    assert "implementation phase · plan 1/1 · task 1/3" in toolbar
    assert "thinking" not in toolbar


def test_mid_step_position_change_re_emits_a_header() -> None:
    drv, out = _driver()
    drv.on_event(ev("1", "change_created", change_id=1, title="t"))
    drv.on_event(ev("2", "step_started", step="task-lead", kind="agent", phase="code",
                     model="m", provider="anthropic", label="task 1/2",
                     phase_label="implementation phase · plan 1/1", position_label=True))
    out.truncate(0)
    out.seek(0)
    drv.on_event(ev("3", "step_label_updated", agent_id="step:code/task-lead",
                     label="task 2/2"))
    body = "\n".join(_body(out))
    assert "implementation phase · plan 1/1 · task 2/2" in body


def test_unchanged_position_push_emits_no_duplicate_header() -> None:
    drv, out = _driver()
    drv.on_event(ev("1", "change_created", change_id=1, title="t"))
    drv.on_event(ev("2", "step_started", step="task-lead", kind="agent", phase="code",
                     model="m", provider="anthropic", label="task 1/2",
                     phase_label="implementation phase · plan 1/1", position_label=True))
    out.truncate(0)
    out.seek(0)
    drv.on_event(ev("3", "step_label_updated", agent_id="step:code/task-lead",
                     label="task 1/2"))           # same position re-pushed
    assert _body(out) == []                       # no duplicate header


def test_verb_step_label_push_emits_no_header() -> None:
    drv, out = _driver()
    drv.on_event(ev("1", "change_created", change_id=1, title="t"))
    drv.on_event(ev("2", "step_started", step="plan-writer", kind="agent", phase="plan",
                     model="m", provider="anthropic", label="writing plan",
                     phase_label="planning phase · plan 2/4"))
    out.truncate(0)
    out.seek(0)
    drv.on_event(ev("3", "step_label_updated", agent_id="step:plan/plan-writer",
                     label="addressing plan review feedback"))   # a verb push, not position
    assert _body(out) == []                       # position unchanged → no header


def test_context_bar_warn_cutoff_follows_checkpoint_threshold() -> None:
    from imp.cli.live_driver import context_bar
    # 164000/200000 = 0.82 — above the 0.80 checkpoint but below the 0.85 default.
    warned = context_bar("sonnet", 164000, checkpoint_threshold=0.8,
                         color=True, over_bg=False)
    default = context_bar("sonnet", 164000, color=True, over_bg=False)
    assert "\x1b[31m" in warned                 # red: past the configured 0.80 warn
    assert "\x1b[31m" not in default            # default 0.85 cutoff -> still yellow
    assert "\x1b[33m" in default


def test_context_bar_absolute_threshold_warns_against_window() -> None:
    from imp.cli.live_driver import context_bar
    # Absolute cap 150000 on a 200000 window -> warn fraction 0.75. 160000 = 0.80
    # is past it -> red.
    bar = context_bar("sonnet", 160000, checkpoint_threshold=150000,
                      color=True, over_bg=False)
    assert "\x1b[31m" in bar


def test_compose_position_bar_forwards_checkpoint_threshold() -> None:
    from imp.cli import narrative as nv
    from imp.cli.live_driver import compose_position_bar
    activity = nv.Activity(model="sonnet",
                           position="implementation phase · plan 1/1 · task 1/3")
    line = compose_position_bar(activity=activity, change_id="abc", title="t",
                                prompt_tokens=164000, color=True, width=200,
                                checkpoint_threshold=0.8)
    assert "\x1b[31m" in line                    # warn forwarded through to the bar

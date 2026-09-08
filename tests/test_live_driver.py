# tests/test_live_driver.py
from __future__ import annotations

import asyncio
import inspect
import io
import json

from imp.cli import narrative as nv
from imp.cli import palette as pal
from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.cli.render_text import LOG_CATEGORIES, render_tree
from imp.cli.signals import InterruptCoordinator
from tests._event_fixtures import _TTY, _two_phase_events, ev


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def _driver():
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    return drv, out


def _body(out: _Pipe) -> list[str]:
    return out.getvalue().splitlines()


def _render_body(events, cats: frozenset[str] = frozenset()) -> list[str]:
    """Full render_tree output. Neither renderer emits a slug/title header any
    more (the transcript opens on the first turn), so the live body and the
    render_tree body are directly comparable line-for-line."""
    tree = nv.build_tree(events)
    return list(render_tree(tree, show=cats, color=False, width=80))


def _cats_body(events, cats: frozenset[str]) -> list[str]:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out),
                     cats=cats)
    for e in events:
        drv.on_event(e)
    drv.finish()
    return out.getvalue().splitlines()


def test_burst_recap_streams_then_prose_in_live_path() -> None:
    drv, out = _driver()
    events = [
        ev("1", "quest_created", quest_id=1, title="x"),
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
    drv.on_event(ev("8", "agent_message", agent_id="a",
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
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="b1", name="grep", input={"q": "x"}),
        ev("5", "tool_result", agent_id="a", id="b1", content="ok"),
        ev("6", "agent_message", agent_id="a",
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
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="t1", name="sub_agent",
           input={"subagent_name": "reviewer"}),
        ev("5", "agent_message", agent_id="sub:4",
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
        ev("1", "quest_created", quest_id=1, title="x"),
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
        ev("1", "quest_created", quest_id=1, title="x"),
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
        ev("1", "quest_created", quest_id=1, title="add auth"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "thinking", agent_id="a", content="real thinking here"),
        ev("5", "tool_call", agent_id="a", id="b1", name="read_file", input={"p": "1"}),
        ev("6", "tool_result", agent_id="a", id="b1", content="ok"),
        ev("7", "agent_message", agent_id="a",
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
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "agent_message", agent_id="a",
           raw={"content": [{"type": "text", "text": "First turn."}]}, usage={}),
        ev("5", "thinking", agent_id="a", content="hmm"),
        ev("6", "agent_message", agent_id="a",   # thinking-only → emits nothing
           raw={"content": [{"type": "thinking", "thinking": "hmm"}]}, usage={}),
        ev("7", "agent_message", agent_id="a",
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
        ev("1", "quest_created", quest_id=1, title="x"),
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
    # The slug/title banner is gone: quest_created (and a later quest_renamed)
    # emit no `#slug  title` line — the transcript opens on the first turn.
    # A rename DOES emit a `• Set name to …` aside (tested in test_rename_line.py).
    drv, out = _driver()
    drv.on_event(ev("1", "quest_created", title="add auth"))
    drv.on_event(ev("2", "quest_renamed", id="ch1", old_title="add auth",
                    new_title="Add OAuth"))
    body = _body(out)
    assert not any(ln.startswith("#") for ln in body)


def test_open_burst_recap_emitted_at_finish() -> None:
    drv, out = _driver()
    for e in [
        ev("1", "quest_created", quest_id=1, title="x"),
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
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="t1", name="sub_agent",
           input={"subagent_name": "reviewer"}),
        ev("5", "agent_message", agent_id="sub:4",
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
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="t1", name="sub_agent",
           input={"subagent_name": "reviewer"}),
        ev("5", "agent_message", agent_id="sub:4",
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
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="t1", name="sub_agent",
           input={"subagent_name": "reviewer"}),
        ev("5", "agent_message", agent_id="sub:4",
           raw={"content": [{"type": "text", "text": "partial work"}]}, usage={}),
    ]:
        drv.on_event(e)
    # Subagent never closes; finish() force-emits its leaf without crashing. The
    # row shows the (still-running) status; internal prose and outcome stay hidden.
    drv.finish()
    body = _body(out)
    assert any(ln.strip() == "reviewer · running" for ln in body)
    assert not any("partial work" in ln for ln in body)
    assert not any("→ status" in ln for ln in body)   # no outcome ever


def test_subagent_body_parity_with_render_tree() -> None:
    # The stated invariant: snapshot (imp log) and live stay BYTE-IDENTICAL via the
    # shared subagent_status_row helper. Both now collapse the subagent to one
    # resolved status row, so assert full-body parity for a subagent that had
    # internal prose AND a structured outcome (neither of which must render).
    events = [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="t1", name="sub_agent",
           input={"subagent_name": "reviewer"}),
        ev("5", "agent_message", agent_id="sub:4",
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
        ev("1", "quest_created", quest_id=1, title="x"),
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
        ev("7", "agent_message", agent_id="sub:4",
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
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="t1", name="sub_agent",
           input={"subagent_name": "reviewer"}),
        # Nested burst left OPEN (no flushing event inside the subagent).
        ev("5", "tool_call", agent_id="sub:4", id="n1", name="bash", input={}),
        ev("6", "tool_result", agent_id="sub:4", id="n1", content="ok"),
        ev("7", "agent_message", agent_id="sub:4",
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
        ev("1", "quest_created", quest_id=1, title="x"),
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
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("3", "step_completed", step="spec", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
        ev("4", "step_started", step="build", kind="agent", phase="implement",
           model="m", provider="anthropic"),
        ev("5", "agent_message", agent_id="a",
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
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="t1", name="sub_agent",
           input={"subagent_name": "reviewer"}),
        ev("5", "agent_message", agent_id="sub:4",
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
        ev("1", "quest_created", quest_id=1, title="t"),
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
        {"type": "quest_created", "agent_id": None, "payload": {"title": "t"},
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
        {"type": "quest_created", "agent_id": None, "payload": {"title": "t"},
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
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "agent_message", agent_id="a",
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
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="t1", name="sub_agent",
           input={"subagent_name": "reviewer"}),
        ev("5", "agent_message", agent_id="sub:4",
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
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "agent_message", agent_id="a",
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


def test_finish_non_tty_adds_no_trailing_blank_after_breadcrumb() -> None:
    events = [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "agent_message", agent_id="a",
           raw={"content": [{"type": "text", "text": "the prose"}]}, usage={}),
        ev("5", "step_completed", step="spec", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ]
    drv, out = _driver()               # _Pipe → non-TTY
    for e in events:
        drv.on_event(e)
    drv.finish()
    body = _body(out)
    assert any("spec · complete" in ln for ln in body)   # breadcrumb newline-terminated, present
    assert body == _render_body(events)                  # live/log parity: no extra trailing blank
    assert not out.getvalue().endswith("\n\n")           # finish() added no non-TTY blank


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
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="spec-writer", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        ev("3", "agent_message", agent_id="a",
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
    # Each body-less chrome step now collapses to a single `<label> · complete` row.
    assert sum("spec-verify-commit" in r for r in rows) == 1
    assert sum("fast-path-gate" in r for r in rows) == 1
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
    drv.on_event(ev("1", "quest_created", quest_id=1, title="x"))
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
    drv.on_event(ev("1", "quest_created", quest_id=1, title="x"))
    drv.on_event(ev("2", "step_started", step="spec", kind="agent", phase="p",
                    model="m", provider="anthropic"))
    out.truncate(0)
    out.seek(0)
    drv._prev_prose = True
    drv._refresh_status()
    assert out.getvalue() == ""
    assert drv._prev_prose is True   # untouched


def test_chrome_step_collapses_live_and_matches_log() -> None:
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
    gate_hits = [ln for ln in body if "gate" in ln]
    assert gate_hits == ["gate · complete"]            # one collapsed line
    assert body == _render_body(events)                # live == imp log


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
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="s", kind="agent", phase="p",
           model="m", provider="anthropic"),
        ev("3", "agent_message", agent_id="a",
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
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="spec", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        ev("3", "agent_message", agent_id="step:spec/spec",
           raw={"content": [{"type": "text", "text": "done"}]}, usage={}),
        ev("4", "step_completed", step="spec", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
        ev("5", "step_started", step="spec-verify-commit", kind="script",
           phase="spec", show_label=False),
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
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="fast-path-spec-gate", kind="branch",
           phase="spec", show_label=False),
        ev("3", "step_completed", step="fast-path-spec-gate", kind="branch",
           action={"verb": "goto", "target": "plan-writer", "resume": False},
           var_mutations={}),
        ev("4", "step_started", step="plan-writer", kind="agent", phase="plan",
           model="m", provider="anthropic"),
        ev("5", "agent_message", agent_id="step:plan/plan-writer",
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
    node = nv.StepNode(name="spec-verify-commit", kind="script", show_label=False)
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
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="merge-to-base", kind="script",
           phase="merge", show_label=False),
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


def test_compose_status_is_flush_left_at_any_frontier_depth() -> None:
    """The transient status line renders flush-left — no gutter indent — even
    while a step is streaming at a deeper frontier."""
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    drv.on_event(ev("1", "step_started", step="s", kind="agent", phase="p"))
    drv.on_event(ev("t1", "tool_call", agent_id="a", id="c1", name="bash",
                    input={"command": "ls"}))
    assert drv._frontier_depth() == 0                  # streaming flush at column 0
    result = drv.compose_status(0)
    assert isinstance(result, list) and len(result) == 3
    assert not result[0].startswith(" ")               # spinner at column 0
    assert result[0][0] == "⠋"
    # position bar is on result[-1]
    assert not result[-1].startswith(" ")



def test_interrupt_breadcrumb_in_live_path() -> None:
    drv, out = _driver()
    for e in [
        ev("1", "quest_created", quest_id=1, title="x"),
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
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="spec-reviewer", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        ev("3", "agent_message", agent_id="step:spec/spec-reviewer",
           raw={"content": [{"type": "text", "text": "approved"}]}, usage={}),
        ev("4", "step_completed", step="spec-reviewer", kind="agent",
           action={"verb": "goto", "target": "plan-writer",
                   "resume": False, "rework": False}, var_mutations={}),
        ev("5", "step_started", step="plan-writer", kind="agent", phase="plan",
           model="m", provider="anthropic"),
        ev("6", "agent_message", agent_id="step:plan/plan-writer",
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


def test_quest_merged_suppressed_in_live_path() -> None:
    drv, out = _driver()
    for e in [
        ev("0", "quest_created", id="run-x",
           title="Run X"),
        ev("1", "step_started", step="merge-to-base",
           kind="merge_and_exit_worktree", phase="review"),
        ev("2", "step_completed", step="merge-to-base", kind="merge_and_exit_worktree",
           action={"verb": "continue"}, var_mutations={}),
        ev("3", "quest_merged", base="master", commit="37dde900abc"),
    ]:
        drv.on_event(e)
    drv.finish()
    body = "\n".join(_body(out))
    # neither the raw event nor the friendly footer is emitted by the live driver
    # (the friendly line is owned by main.py _emit_completed).
    assert "quest_merged" not in body
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
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="spec-reviewer", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        ev("3", "agent_message", agent_id="step:spec/spec-reviewer",
           provider="anthropic", model="m", usage={},
           raw={"content": [{"type": "text", "text": narration}]}),
        ev("4", "tool_call", agent_id="step:spec/spec-reviewer", id="cs1",
           name="complete_step",
           input={"status": "Success", "result": "DONE", "findings": findings}),
        ev("5", "tool_result", agent_id="step:spec/spec-reviewer", id="cs1",
           content="ok"),
        ev("6", "agent_result", agent_id="step:spec/spec-reviewer",
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
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="spec-writer", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        # First narration — held.
        ev("3", "agent_message", agent_id="step:spec/spec-writer",
           raw={"content": [{"type": "text", "text": "First narration."}]}),
        # A thinking turn + a tool burst separate it from the next narration —
        # exactly what `_last_narration_index` cannot scan past.
        ev("4", "thinking", agent_id="step:spec/spec-writer", content="hmm"),
        ev("5", "tool_call", agent_id="step:spec/spec-writer", id="b1",
           name="grep", input={"q": "x"}),
        ev("6", "tool_result", agent_id="step:spec/spec-writer", id="b1",
           content="ok"),
        # Second narration — the new trailing block; must un-hold the first.
        ev("7", "agent_message", agent_id="step:spec/spec-writer",
           raw={"content": [{"type": "text", "text": "Second narration."}]}),
        ev("8", "tool_call", agent_id="step:spec/spec-writer", id="cs1",
           name="complete_step", input={"status": "Success"}),
        ev("9", "tool_result", agent_id="step:spec/spec-writer", id="cs1",
           content="ok"),
        ev("10", "agent_result", agent_id="step:spec/spec-writer", status="Success"),
        ev("11", "step_completed", step="spec-writer", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
        # A SECOND step. Before the fix its header never rendered live because the
        # held opening narration blocked the cursor.
        ev("12", "step_started", step="plan-writer", kind="agent", phase="plan",
           model="m", provider="anthropic"),
        ev("13", "agent_message", agent_id="step:plan/plan-writer",
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


def test_labeled_route_reason_rides_target_header_in_both_paths() -> None:
    drv, out = _driver()
    events = [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="code-reviewer", kind="agent", phase="review",
           model="m", provider="anthropic"),
        ev("3", "agent_message", agent_id="step:review/code-reviewer",
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
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("3", "agent_message", agent_id="a",
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
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="spec-reviewer", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        ev("3", "agent_message", agent_id="step:spec/spec-reviewer",
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


def test_live_parallel_batch_rows_show_correct_elapsed() -> None:
    # Tripwire: a streaming chunk must not resume-close sibling folds, freezing their elapsed.
    drv, out = _driver()
    for e in [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("tk1", "tool_call", agent_id="a", id="b1", name="sub_agent",
           input={"subagent_name": "reviewer"},
           timestamp="2026-06-19T10:00:00+00:00"),
        ev("tk2", "tool_call", agent_id="a", id="b2", name="sub_agent",
           input={"subagent_name": "tester"},
           timestamp="2026-06-19T10:00:01+00:00"),
        ev("a1", "agent_message", agent_id="a", stop_reason=None, usage={},
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
        ev("1", "quest_created", quest_id=1, title="x"),
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
    drv.on_event(ev("1", "quest_created", quest_id=1, title="x"))
    assert drv.prompt_indent() == ""


def test_multiphase_subagent_reentry_live_log_parity() -> None:
    # A multi-phase run with a subagent and an implement-loop re-entry: the live body
    # must be byte-equal to the imp log body, the re-entry must print a second
    # `── implement phase ───` start rule, and a closed phase's end rule must land at column 0.
    events = [
        ev("e0", "quest_created", quest_id=1, title="t", timestamp="2026-06-03T00:00:00+00:00"),
        ev("e1", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic", timestamp="2026-06-03T00:00:00+00:00"),
        ev("e2", "step_completed", step="spec", kind="agent",
           action={"verb": "continue"}, var_mutations={}, timestamp="2026-06-03T00:00:01+00:00"),
        ev("e3", "step_started", step="build", kind="agent", phase="implement",
           model="m", provider="anthropic", timestamp="2026-06-03T00:00:01+00:00"),
        ev("e4", "tool_call", agent_id="a", id="t1", name="sub_agent",
           input={"subagent_name": "reviewer"}),
        ev("e5", "agent_message", agent_id="sub:e4",
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

def _opening_intent_events():
    # opening_intent is emitted BEFORE the first step_started, so its
    # UserMessageNode lands at the workflow root, before the first phase header.
    return [
        {"type": "quest_created", "agent_id": None, "payload": {"title": "t"},
         "id": "e0", "timestamp": "2026-06-03T00:00:00+00:00"},
        {"type": "user_message", "agent_id": None,
         "payload": {"role": "user", "subtype": "intent",
                     "content": [{"type": "text", "text": "build the parser"}]},
         "id": "e1", "timestamp": "2026-06-03T00:00:00+00:00"},
        {"type": "step_started", "agent_id": None,
         "payload": {"step": "draft", "kind": "branch", "phase": "spec"},
         "id": "e2", "timestamp": "2026-06-03T00:00:00+00:00"},
        {"type": "step_completed", "agent_id": None,
         "payload": {"step": "draft", "kind": "branch", "action": {"verb": "continue"}},
         "id": "e3", "timestamp": "2026-06-03T00:00:05+00:00"},
    ]


def test_opening_intent_does_not_drop_following_step_output() -> None:
    # Root-placement world (was change fbb6r4): the opening_intent UserMessageNode
    # lives at the workflow root, ahead of the first phase, and emits immediately.
    # It is never re-homed under the first step, so the monotonic-cursor skip that
    # once blanked the live view cannot occur. BOTH the opening prompt AND the
    # agent's output must render live.
    drv, out = _driver()
    events = [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("e1", "user_message", role="user", subtype="intent",
           content=[{"type": "text", "text": "build the parser"}]),
        ev("3", "step_started", step="spec-writer", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        ev("8", "agent_message", agent_id="a",
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
    assert any("drafting the spec now" in ln for ln in body)  # step output
    # Placement: opening intent is a child of root, not under the step.
    tree = drv._builder.tree
    intents = [c for c in tree.root.children if isinstance(c, nv.UserMessageNode)]
    assert len(intents) == 1
    phase = next(c for c in tree.root.children if isinstance(c, nv.PhaseNode))
    step = next(c for c in phase.children if isinstance(c, nv.StepNode))
    assert not any(isinstance(c, nv.UserMessageNode) for c in step.children)
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
    for e in marked[:3]:
        drv.on_event(e)
    drv.on_event({"type": "replay_complete"})   # boundary closes the replay
    for e in marked[3:]:
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
    for e in marked[:3]:
        sdrv.on_event(e)
    sdrv.on_event({"type": "replay_complete"})   # boundary closes the replay
    for e in marked[3:]:
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
    for e in replayed:
        drv.on_event(e)
    drv.on_event({"type": "replay_complete"})   # boundary closes the replay
    for e in live:
        drv.on_event(e)
    drv.finish()
    body = _body(out)
    assert not any("build the parser" in ln for ln in body)
    assert any(ln == "── plan phase ───" for ln in body)
    # Root-placement world: the replayed opening intent lives at root (visually
    # suppressed here, not re-homed under a step).
    tree = drv._builder.tree
    intents = [c for c in tree.root.children if isinstance(c, nv.UserMessageNode)]
    assert len(intents) == 1


def test_suppress_replay_with_no_replay_prefix_renders_full_history() -> None:
    events = _two_phase_events()
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80,
                     status=LiveStatus(out), suppress_replay=True)
    drv.on_event({"type": "replay_complete"})   # empty replayed prefix
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
    drv.on_event({"type": "replay_complete"})   # boundary, then zero live events
    drv.finish()
    assert all(not ln for ln in _body(out))


def test_replay_complete_boundary_lights_footer_without_live_event() -> None:
    """The replay_complete boundary is the SOLE footer-light point: on a TTY
    resume the footer stays dark through the _replay-marked prefix, then lights
    the instant the boundary arrives — WITHOUT any further live event. This is the
    merge-queue-wait case: the live wait-for-head step emits nothing, so the
    footer must wake on the boundary alone or the resume shows a blank line."""
    out = _TTYPipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out),
                     suppress_replay=True)
    # Replayed prefix (marked _replay): a merge-queue wait whose lane_waiting sets
    # the running step's verb. current_activity() is non-None, so an un-decoupled
    # footer WOULD have drawn here during replay.
    replayed = [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="wait-for-head", kind="acquire",
           phase="integration", label="waiting in merge queue"),
        ev("3", "lane_waiting", agent_id="step:integration/wait-for-head", ahead=5),
    ]
    for e in replayed:
        drv.on_event({**e, "_replay": True})
    assert drv._builder.current_activity() is not None   # footer WOULD draw if unguarded
    assert out.getvalue() == ""                          # dark through the replayed prefix
    # The boundary lights the footer with NO live event following it.
    drv.on_event({"type": "replay_complete"})
    body = out.getvalue()
    assert body != ""                                    # footer paints at the boundary
    assert "waiting to merge" in body                    # the running step's verb
    assert "malformed" not in body                       # marker never becomes an ErrorNode


def test_replay_complete_marker_feeds_no_builder_node_cli() -> None:
    """AC (d), CLI: the boundary is intercepted before _builder.add, so it adds
    no tree node (and no malformed-payload ErrorNode) and renders nothing."""
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out),
                     suppress_replay=True)
    drv.on_event({**ev("1", "quest_created", quest_id=1, title="x"), "_replay": True})
    nodes_before = len(drv._snapshot_node_ids())
    drv.on_event({"type": "replay_complete"})
    assert len(drv._snapshot_node_ids()) == nodes_before   # marker created no node
    assert out.getvalue() == ""                            # and rendered nothing


def test_footer_live_from_start_without_suppression() -> None:
    """AC (e): imp new/chat/log --follow construct LiveDriver with the default
    suppress_replay=False, so _footer_live starts True and the footer draws from
    the first event with no boundary ever received."""
    out = _TTYPipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    assert drv._footer_live is True
    for e in [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="spec", kind="agent", phase="p",
           model="m", provider="anthropic"),
    ]:
        drv.on_event(e)
    assert out.getvalue() != ""   # footer painted with no boundary needed


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
    for e in marked[:3]:
        drv.on_event(e)
    drv.on_event({"type": "replay_complete"})   # boundary closes the replay
    for e in marked[3:]:
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
    for e in marked[:3]:
        drv.on_event(e)
    drv.on_event({"type": "replay_complete"})   # boundary closes the replay
    for e in marked[3:]:
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
        ev("1", "quest_created", quest_id=1, title="t"),
        ev("2", "step_started", step="spec", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        ev("2b", "awaiting_user_input", agent_id="step:spec/spec"),
        ev("3", "user_message", content=[{"type": "text", "text": "do the thing"}]),
    ]
    marked = [{**e, "_replay": True} for e in events[:3]] + events[3:]
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out),
                     suppress_replay=True, preceded_by_blank=True)
    for e in marked[:3]:
        drv.on_event(e)
    drv.on_event({"type": "replay_complete"})   # boundary closes the replay
    for e in marked[3:]:
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


def test_context_bar_renders_a_ten_cell_block_gauge_and_the_percentage() -> None:
    from imp.cli.live_driver import context_bar
    bar = context_bar("anthropic", "sonnet", 26000, color=False, over_bg=False)
    assert bar == "█░░░░░░░░░ 13%"      # 26000/200000 = 0.13 -> one filled cell


def test_context_bar_gauge_spans_empty_to_full() -> None:
    from imp.cli.live_driver import context_bar

    def gauge(pct: float) -> str | None:
        return context_bar("anthropic", "sonnet", None, context_pct=pct,
                           color=False, over_bg=False)

    assert gauge(0.0) == "░░░░░░░░░░ 0%"
    assert gauge(44.0) == "████░░░░░░ 44%"
    assert gauge(100.0) == "██████████ 100%"   # full: no track cells at all


def test_context_gauge_fill_and_percentage_share_one_hue_over_a_gray_track() -> None:
    from imp.cli import palette as pal
    from imp.cli.live_driver import context_bar
    # sonnet: 200k window, 0.8 cutoff -> wordmark from 0.60, red from 0.80. Both
    # 119000 and 121000 label "60%" and differ only in hue, straddling the amber
    # boundary; 160000 sits exactly on the red one.
    try:
        pal.set_theme("ansi")
        green = context_bar("anthropic", "sonnet", 119000, color=True, over_bg=False)
        amber = context_bar("anthropic", "sonnet", 121000, color=True, over_bg=False)
        red = context_bar("anthropic", "sonnet", 160000, color=True, over_bg=False)
    finally:
        pal.set_theme("ansi")
    assert green == "\x1b[32m██████\x1b[0m\x1b[90m░░░░\x1b[0m \x1b[32m60%\x1b[0m"
    assert amber == "\x1b[33m██████\x1b[0m\x1b[90m░░░░\x1b[0m \x1b[33m60%\x1b[0m"
    assert red == "\x1b[31m████████\x1b[0m\x1b[90m░░\x1b[0m \x1b[31m80%\x1b[0m"


def test_context_bar_bands_are_relative_to_the_cutoff() -> None:
    from imp.cli.live_driver import context_bar
    # sonnet has no soft_cap: the cutoff is the 0.8 default, so amber starts at
    # 0.75 * 0.8 = 0.60 (today's green boundary) and red at the 0.8 handoff.
    green = context_bar("anthropic", "sonnet", 100000, color=True, over_bg=False)
    amber = context_bar("anthropic", "sonnet", 140000, color=True, over_bg=False)
    red = context_bar("anthropic", "sonnet", 180000, color=True, over_bg=False)
    assert green is not None and amber is not None and red is not None
    assert "\x1b[32m" in green                     # 50% -> green fill
    assert "\x1b[33m" in amber                     # 70% -> wordmark fill
    assert "\x1b[31m" in red                       # 90% -> red fill
    # Boundaries: 0.595 is green, 0.605 is amber, 0.80 (the cutoff itself) is
    # red. Both boundary bars LABEL "60%" and differ only in hue — the amber
    # edge is asserted off the exact 0.6 ratio on purpose, since `0.75 * 0.8` is
    # 0.6000000000000001 in doubles, so a bar at precisely 0.6 falls green by
    # one ulp. Behaviourally invisible, and only at that one exact ratio.
    just_green = context_bar("anthropic", "sonnet", 119000, color=True, over_bg=False)
    boundary_amber = context_bar("anthropic", "sonnet", 121000, color=True, over_bg=False)
    boundary_red = context_bar("anthropic", "sonnet", 160000, color=True, over_bg=False)
    assert just_green is not None and boundary_amber is not None
    assert boundary_red is not None
    assert "\x1b[32m" in just_green and "\x1b[33m" not in just_green
    assert "\x1b[33m" in boundary_amber and "\x1b[32m" not in boundary_amber
    assert "\x1b[31m" in boundary_red and "\x1b[33m" not in boundary_red


# AC 9(a)
def test_context_bar_bands_follow_a_capped_model_down() -> None:
    from imp.cli.live_driver import context_bar
    # The regression this quest exists for: gpt-5.6-sol hands off at 260k of a
    # 1.05M window (~24.8%), so 30% and 50% are RED (they rendered green before,
    # because the old green band was tested first and swallowed any lower mark).
    at_30 = context_bar("openai", "gpt-5.6-sol", 315_000, color=True, over_bg=False)
    at_50 = context_bar("openai", "gpt-5.6-sol", 525_000, color=True, over_bg=False)
    at_19 = context_bar("openai", "gpt-5.6-sol", 199_500, color=True, over_bg=False)
    at_10 = context_bar("openai", "gpt-5.6-sol", 105_000, color=True, over_bg=False)
    assert at_30 is not None and at_50 is not None
    assert at_19 is not None and at_10 is not None
    assert "\x1b[31m" in at_30 and at_30.endswith("30%\x1b[0m")
    assert "\x1b[31m" in at_50
    assert "\x1b[33m" in at_19 and "\x1b[31m" not in at_19   # 19% >= 0.75 * 24.8%
    assert "\x1b[32m" in at_10


# AC 9(b)
def test_context_bar_uncapped_1m_model_keeps_the_default_bands() -> None:
    from imp.cli.live_driver import context_bar
    green = context_bar("anthropic", "claude-opus-5[1m]", 500_000,
                        color=True, over_bg=False)
    amber = context_bar("anthropic", "claude-opus-5[1m]", 700_000,
                        color=True, over_bg=False)
    red = context_bar("anthropic", "claude-opus-5[1m]", 800_000,
                      color=True, over_bg=False)
    assert green is not None and amber is not None and red is not None
    assert "\x1b[32m" in green                     # 50%: the OpenAI cap must not leak
    assert "\x1b[33m" in amber                     # 70%
    assert "\x1b[31m" in red                       # 80% = the handoff point


def test_context_bar_midrange_fill_is_wordmark_under_dark_theme() -> None:
    from imp.cli import palette as pal
    from imp.cli.live_driver import context_bar
    try:
        pal.set_theme("dark")
        bar = context_bar("anthropic", "sonnet", 140000, color=True, over_bg=False)
        assert bar is not None
        assert pal.sgr("wordmark", enabled=True) in bar   # \x1b[38;2;255;154;60m
        assert pal.sgr("yellow", enabled=True) not in bar  # not the muted yellow role
    finally:
        pal.set_theme("ansi")


def test_context_bar_resolves_a_dated_id_through_limits_for() -> None:
    from imp.cli.live_driver import context_bar
    from imp.providers.catalog import limits_for
    dated = limits_for("anthropic", "claude-opus-5-20260315")
    assert dated is not None
    assert dated.window == 200_000                 # longest-prefix, not substring
    assert context_bar("anthropic", "claude-opus-5-20260315", 40000,
                       color=False, over_bg=False) == "██░░░░░░░░ 20%"


def test_context_bar_prefers_sdk_percentage_over_tokens() -> None:
    from imp.cli.live_driver import context_bar
    # context_pct is rendered directly: a huge prompt_tokens that would imply
    # ~100% is ignored when the SDK percentage is present.
    bar = context_bar("claude_code", "claude-opus-4-8[1m]", 999_999,
                      context_pct=42.0, color=False, over_bg=False)
    assert bar == "████░░░░░░ 42%"


def test_live_driver_has_no_context_windows_symbol() -> None:
    import imp.cli.live_driver as ld
    assert not hasattr(ld, "_CONTEXT_WINDOWS")
    assert not hasattr(ld, "context_window")


def test_context_bar_omitted_for_unknown_pair_or_missing_usage() -> None:
    from imp.cli.live_driver import context_bar
    assert context_bar("openai", "some-unlisted-model", 1000,
                       color=False, over_bg=False) is None
    assert context_bar("anthropic", None, 1000, color=False, over_bg=False) is None
    assert context_bar("anthropic", "sonnet", None, color=False, over_bg=False) is None


# AC 9(c)
def test_context_bar_unresolvable_provider_draws_nothing_on_the_tokens_path() -> None:
    from imp.cli.live_driver import context_bar
    # No provider -> no window -> no bar, rather than a bar whose mark comes
    # from a guessed window. The live context_pct path needs no window and
    # still draws, at the 60/80 default bands.
    assert context_bar(None, "sonnet", 100000, color=False, over_bg=False) is None
    assert context_bar("mystery", "sonnet", 100000, color=False, over_bg=False) is None
    amber = context_bar(None, "sonnet", None, context_pct=70.0,
                        color=True, over_bg=False)
    red = context_bar(None, "sonnet", None, context_pct=80.0,
                      color=True, over_bg=False)
    assert amber is not None and red is not None
    assert "\x1b[33m" in amber
    assert "\x1b[31m" in red


def test_position_row_separates_model_from_the_gauge_with_a_middle_dot() -> None:
    from imp.cli.live_driver import compose_position_row, context_bar
    act = nv.Activity(kind="agent", model="sonnet", provider="anthropic",
                      phase="review")
    line = compose_position_row(activity=act, quest_id="z9gixx", title="t",
                                prompt_tokens=26000, color=False, width=200)
    bar = context_bar("anthropic", "sonnet", 26000, color=False, over_bg=False)
    assert bar is not None
    assert f"sonnet · {bar}" in line          # model and gauge are two fields now
    assert "sonnet context" not in line       # never space-bound on this row


def test_subagent_row_orders_elapsed_before_model_graph() -> None:
    from imp.cli import render_text as rt
    from imp.cli.live_driver import compose_subagent_rows, context_bar
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.add(ev("2", "step_started", step="s", kind="agent", phase="p",
             model="m", provider="anthropic"))
    b.add(ev("t1", "tool_call", agent_id="step:p/s", id="blk-t1",
             name="sub_agent", input={"subagent_name": "coder"},
             timestamp="2026-01-01T00:00:00+00:00"))
    b.add(ev("c1", "step_context", agent_id="sub:t1", provider="anthropic",
             model="sonnet", tools_schema=[], params={}, max_tool_calls=None))
    b.add(ev("r1", "agent_message", agent_id="sub:t1", provider="anthropic",
             model="sonnet", stop_reason="end_turn",
             raw={"content": [{"type": "text", "text": "hi"}]},
             usage={"input_tokens": 60000, "output_tokens": 5,
                    "cache_read_input_tokens": 20000,
                    "cache_creation_input_tokens": 0},
             context_pct=42.0, context_window=200_000))
    f = b.running_subagents()[0]
    model = rt.subagent_model(f)
    provider = rt.subagent_provider(f)
    assert (provider, model) == ("anthropic", "sonnet")   # sanity: pair resolvable
    bar = context_bar(provider, model, f.nested.latest_prompt_tokens,
                      context_pct=f.nested.latest_context_pct,
                      color=False, over_bg=False)
    assert bar is not None
    rows = compose_subagent_rows(b, color=False, width=200,
                                 now_iso="2026-01-01T00:00:05+00:00")
    assert f"sonnet · {bar}" in rows[0]     # · separates model from gauge
    # new segment order: role · elapsed · model · context-bar
    assert "coder · 5s · sonnet" in rows[0]              # role · elapsed · model, in order
    assert rows[0].index("5s") < rows[0].index("sonnet")  # elapsed precedes model
    assert rows[0].rstrip().endswith(bar)                # model context-bar now trailing


def test_compose_toolbar_static_uses_frame_zero_and_no_elapsed() -> None:
    out = _Pipe()
    status = LiveStatus(out)
    drv = LiveDriver(stream=out, color=False, width=80, status=status)
    drv.on_event(ev("1", "quest_created", quest_id=1, title="t"))
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


def test_compose_toolbar_static_identity_bar_before_first_activity() -> None:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    # A change id exists (quest_created replayed) but no step has started yet.
    drv.on_event(ev("1", "quest_created", id="z9gixx", title="Fix bug"))
    toolbar = drv.compose_toolbar_static()
    assert toolbar == "z9gixx · Fix bug"   # minimal identity bar, no phase/model


def test_compose_toolbar_static_identity_bar_degrades_to_id_when_unnamed() -> None:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    drv.on_event(ev("1", "quest_created", id="z9gixx"))   # no title yet
    toolbar = drv.compose_toolbar_static()
    assert toolbar == "z9gixx"             # unnamed change -> id alone


def test_compose_identity_toolbar_carries_slate_and_padding_under_dark_theme() -> None:
    from imp.cli.live_driver import compose_identity_toolbar
    try:
        pal.set_theme("dark")
        bar = compose_identity_toolbar(quest_id="z9gixx", title="t",
                                       color=True, width=120)
    finally:
        pal.set_theme("ansi")
    assert bar is not None
    assert bar.startswith("\x1b[48;2;43;47;58m")   # same cool blue-gray band as the position row
    assert bar.endswith("\x1b[0m")
    assert pal.display_width(bar) == 118           # padded to width-2, aligned with the footer


def test_compose_identity_toolbar_title_is_unpainted_under_ansi_theme() -> None:
    from imp.cli.live_driver import compose_identity_toolbar
    try:
        pal.set_theme("ansi")
        bar = compose_identity_toolbar(quest_id="z9gixx", title="t",
                                       color=True, width=120)
    finally:
        pal.set_theme("ansi")
    assert bar is not None
    parts = bar.split(" · ")
    # id · title; under ansi the band bg is empty, so gray would be the only \x1b[90m.
    assert "\x1b[33m" in parts[0]                # id still wordmark gold
    assert "\x1b[90m" not in bar                 # title no longer gray-painted
    assert "\x1b[" not in parts[1]               # title: unpainted default fg


def test_compose_identity_toolbar_bands_a_title_without_a_quest_id() -> None:
    from imp.cli.live_driver import compose_identity_toolbar
    # A title and no quest id: the band names it.
    assert compose_identity_toolbar(quest_id=None, title="imp setup",
                                    color=False, width=80) == "imp setup"
    # Nothing to name at all -> no band.
    assert compose_identity_toolbar(quest_id=None, title="",
                                    color=False, width=80) is None


def test_live_driver_caps_hr_at_display_width() -> None:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=10000, display_width=40,
                     status=LiveStatus(out))
    long_line = "X" * 200
    drv.on_event(ev("1", "quest_created", quest_id=1, title="x"))
    drv.on_event(ev("2", "step_started", step="s", kind="agent", phase="p",
                    model="m", provider="anthropic"))
    drv.on_event(ev("3", "agent_message", agent_id="a", usage={},
                    raw={"content": [{"type": "text",
                                      "text": long_line + "\n\n---\n\nafter"}]}))
    drv.on_event(ev("4", "step_completed", step="s", kind="agent",
                    action={"verb": "continue"}, var_mutations={}))
    body = _body(out)
    assert any(long_line in ln for ln in body)        # prose unwrapped (wrap width)
    assert any(ln.count("─") == 40 for ln in body)    # HR capped to the display width
    assert not any(ln.count("─") > 40 for ln in body)


def test_compose_activity_line_is_verb_detail_elapsed_with_spinner() -> None:
    from imp.cli.live_driver import compose_activity_line
    act = nv.Activity(verb="coding", detail="bash pytest", model="m",
                      start_ts="2026-01-01T00:00:00+00:00")
    line = compose_activity_line(activity=act, frame=0, color=False,
                                 now_iso="2026-01-01T00:00:14+00:00")
    assert line is not None
    assert "coding" in line and "bash pytest" in line and "14s" in line


def test_compose_activity_line_animates_spinner_and_elapsed_when_verbless() -> None:
    from imp.cli.live_driver import compose_activity_line
    act = nv.Activity(position="implementation phase · plan 1/1", verb=None, detail=None,
                      start_ts="2026-01-01T00:00:00+00:00")
    line = compose_activity_line(activity=act, frame=0, color=False,
                                 now_iso="2026-01-01T00:00:04+00:00")
    assert line == "⠋ 4s"


def test_compose_activity_line_spinner_uses_wordmark_and_no_band_bg() -> None:
    from imp.cli.live_driver import compose_activity_line
    try:
        for theme in ("dark", "light", "ansi"):
            pal.set_theme(theme)
            act = nv.Activity(verb="thinking", start_ts="2026-01-01T00:00:00+00:00")
            line = compose_activity_line(activity=act, frame=0, color=True,
                                         now_iso="2026-01-01T00:00:04+00:00")
            # The activity line uses the wordmark color for the spinner — never cyan.
            # No background prefix on the activity line (background is on the position bar).
            assert pal._ANSI["wordmark"] in line             # wordmark spinner present
            assert pal._ANSI["cyan"] not in line             # no longer cyan
            bg = pal.toolbar_bg_open(enabled=True)
            if bg:  # only check when the theme has a bg color
                assert not line.startswith(bg)               # no bg prefix on activity line
    finally:
        pal.set_theme("ansi")


def test_compose_status_returns_activity_line_blank_then_position_bar() -> None:
    drv, _ = _driver()
    drv.on_event(ev("1", "quest_created", quest_id=1, title="t"))
    drv.on_event(ev("2", "step_started", step="task-lead", kind="agent", phase="code",
                     model="claude-sonnet-4-5", provider="anthropic", label="task 1/3",
                     phase_label="implementation phase · plan 1/1", position_label=True))
    block = drv.compose_status(0)
    assert isinstance(block, list) and len(block) == 3
    assert "thinking" in block[0]
    assert block[1] == ""
    assert "implementation phase · plan 1/1 · task 1/3" in block[2]
    assert "thinking" not in block[2]


def test_compose_toolbar_static_uses_the_position_row_layout() -> None:
    drv, _ = _driver()
    drv.on_event(ev("1", "quest_created", id="z9gixx", title="t"))
    drv.on_event(ev("2", "step_started", step="task-lead", kind="agent", phase="code",
                     model="claude-sonnet-4-5", provider="anthropic", label="task 1/3",
                     phase_label="implementation phase · plan 1/1", position_label=True))
    toolbar = drv.compose_toolbar_static()
    assert toolbar is not None
    assert "implementation phase · plan 1/1 · task 1/3" in toolbar
    assert "thinking" not in toolbar
    # Flat, identity-first: id·title precede the structural group.
    assert toolbar.index("z9gixx") < toolbar.index("implementation phase")


def test_mid_step_position_change_re_emits_a_header() -> None:
    drv, out = _driver()
    drv.on_event(ev("1", "quest_created", quest_id=1, title="t"))
    drv.on_event(ev("2", "step_started", step="task-lead", kind="agent", phase="code",
                     model="m", provider="anthropic", label="task 1/2",
                     phase_label="implementation phase · plan 1/1", position_label=True))
    out.truncate(0)
    out.seek(0)
    drv.on_event(ev("3", "step_label_updated", agent_id="step:code/task-lead",
                     label="task 2/2"))
    body = "\n".join(_body(out))
    assert "task 2/2" in body                          # new position re-emitted
    assert "implementation phase · plan 1/1 · task 2/2" in body  # phase now prefixes the header


def test_reemitted_header_marks_an_in_session_resume() -> None:
    # awaiting_user renders no PauseNode, so the pause is invisible in the
    # scrollback — and there is no workflow_resumed on this surface at all.
    drv, out = _driver()
    drv.on_event(ev("1", "quest_created", quest_id=1, title="t"))
    drv.on_event(ev("2", "step_started", step="task-lead", kind="agent", phase="code",
                    model="m", provider="anthropic", label="task 1/2",
                    phase_label="implementation phase · plan 1/1", position_label=True))
    drv.on_event(ev("3", "workflow_paused", reason="awaiting_user"))
    drv.on_event(ev("4", "step_started", step="task-lead", kind="agent", phase="code",
                    model="m", provider="anthropic", label="task 1/2",
                    phase_label="implementation phase · plan 1/1", position_label=True,
                    resumed=True))
    out.truncate(0)
    out.seek(0)
    drv.on_event(ev("5", "step_label_updated", agent_id="step:code/task-lead",
                    label="task 2/2"))
    body = "\n".join(_body(out))
    assert "implementation phase · plan 1/1 · task 2/2 (resume)" in body


def test_reemitted_header_does_not_mark_a_routed_reentry() -> None:
    drv, out = _driver()
    drv.on_event(ev("1", "quest_created", quest_id=1, title="t"))
    drv.on_event(ev("2", "step_started", step="task-lead", kind="agent", phase="code",
                    model="m", provider="anthropic", label="task 1/2",
                    phase_label="implementation phase · plan 1/1", position_label=True,
                    resumed=True, route_label="fixing test failures"))
    out.truncate(0)
    out.seek(0)
    drv.on_event(ev("3", "step_label_updated", agent_id="step:code/task-lead",
                    label="task 2/2"))
    body = "\n".join(_body(out))
    assert "task 2/2" in body               # the header WAS re-emitted
    assert "(resume)" not in body


def test_unchanged_position_push_emits_no_duplicate_header() -> None:
    drv, out = _driver()
    drv.on_event(ev("1", "quest_created", quest_id=1, title="t"))
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
    drv.on_event(ev("1", "quest_created", quest_id=1, title="t"))
    drv.on_event(ev("2", "step_started", step="plan-writer", kind="agent", phase="plan",
                     model="m", provider="anthropic", label="writing plan",
                     phase_label="planning phase · plan 2/4"))
    out.truncate(0)
    out.seek(0)
    drv.on_event(ev("3", "step_label_updated", agent_id="step:plan/plan-writer",
                     label="addressing plan review feedback"))   # a verb push, not position
    assert _body(out) == []                       # position unchanged → no header


# AC 9(d): the step-row surface
def test_position_row_bands_the_bar_from_the_activity_provider() -> None:
    from imp.cli.live_driver import compose_position_row
    # Same model, same occupancy (30% of the 1.05M window): with the provider
    # the cap resolves and the bar is red; without it nothing resolves and the
    # tokens path draws no bar at all.
    banded = compose_position_row(
        activity=nv.Activity(kind="agent", model="gpt-5.6-sol", provider="openai",
                             phase="implementation phase"),
        quest_id="abc", title="t", prompt_tokens=315_000, color=True, width=200)
    unresolved = compose_position_row(
        activity=nv.Activity(kind="agent", model="gpt-5.6-sol", phase="implementation phase"),
        quest_id="abc", title="t", prompt_tokens=315_000, color=True, width=200)
    assert "\x1b[31m" in banded and "30%" in banded
    assert "%" not in unresolved


# AC 9(d): the subagent-row surface
def test_subagent_row_bands_identically_to_its_step_row() -> None:
    from imp.cli.live_driver import compose_subagent_rows
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.add(ev("2", "step_started", step="s", kind="agent", phase="p",
             model="gpt-5.6-sol", provider="openai"))
    b.add(ev("t1", "tool_call", agent_id="step:p/s", id="blk-t1",
             name="sub_agent", input={"subagent_name": "coder"},
             timestamp="2026-01-01T00:00:00+00:00"))
    b.add(ev("c1", "step_context", agent_id="sub:t1", provider="openai",
             model="gpt-5.6-sol", tools_schema=[], params={},
             max_tool_calls=None))
    b.add(ev("r1", "agent_message", agent_id="sub:t1", provider="openai",
             model="gpt-5.6-sol", stop_reason="end_turn",
             raw={"content": [{"type": "text", "text": "hi"}]},
             usage={"input_tokens": 315_000, "output_tokens": 5}))
    rows = compose_subagent_rows(b, color=True, width=200,
                                 now_iso="2026-01-01T00:00:05+00:00")
    # 315000 / 1.05M = 30%, past the 260k cap -> red on the subagent row too.
    assert "30%" in rows[0]
    assert "\x1b[31m" in rows[0]


def test_completed_node_suppressed_in_live_leaf() -> None:
    drv, out = _driver()
    drv._emit_leaf(nv.CompletedNode(quest_id="abc123"), 0)
    # main.py _emit_completed owns the friendly line; only its blank-above is emitted here
    assert out.getvalue() == "\n"
    assert "abc123" not in out.getvalue()


def test_merged_node_still_suppressed_in_live_leaf() -> None:
    drv, out = _driver()
    drv._emit_leaf(nv.MergedNode(quest_id="abc123", base="master"), 0)
    assert out.getvalue() == "\n"
    assert "abc123" not in out.getvalue()
    assert "master" not in out.getvalue()


def test_show_label_false_script_silent_live_and_matches_log() -> None:
    drv, out = _driver()
    events = [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="spec", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        ev("3", "agent_message", agent_id="step:spec/spec",
           raw={"content": [{"type": "text", "text": "done"}]}, usage={}),
        ev("4", "step_completed", step="spec", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
        ev("5", "step_started", step="cleanup", kind="script", phase="spec",
           show_label=False),
        ev("6", "step_completed", step="cleanup", kind="script",
           action={"verb": "continue"}, var_mutations={}),
    ]
    for e in events:
        drv.on_event(e)
    drv.finish()
    assert not any("cleanup" in ln for ln in _body(out))
    assert _body(out) == _render_body(events)   # live == imp log, line for line


def test_show_label_false_script_reveals_frame_on_pause_live_and_matches_log() -> None:
    # Live/log parity for the non-linear-outcome path: a show_label=False step that
    # PAUSES must surface its full block in the live stream exactly as `imp log`
    # renders it — not get silently suppressed like the deferred-leaf continue path.
    drv, out = _driver()
    events = [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="merge-guard", kind="script", phase="merge",
           show_label=False),
        ev("3", "error", message="boom in bar.py"),
        ev("4", "step_completed", step="merge-guard", kind="script",
           action={"verb": "pause"}, var_mutations={}),
    ]
    for e in events:
        drv.on_event(e)
    drv.finish()
    body = _body(out)
    assert any("merge-guard" in ln for ln in body)   # frame revealed on pause
    assert body == _render_body(events)              # live == imp log, line for line


def test_terminal_nested_pause_renders_live_and_matches_render_tree() -> None:
    # Pause fires after step_completed (mid-phase): the PauseNode nests as a phase
    # child. finish()'s force-walk must reach and emit it, and the live body must
    # match render_tree line-for-line (live == imp log).
    events = [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("3", "step_completed", step="spec", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
        ev("4", "workflow_paused", reason="max_loops", interrupt_reason="max_loops"),
    ]
    drv, out = _driver()
    for e in events:
        drv.on_event(e)
    drv.finish()
    body = _body(out)
    assert any("paused — a step kept sending work back" in ln for ln in body)
    assert body == _render_body(events)


def test_terminal_pause_flushes_warnings_summary_in_live() -> None:
    # finish() calls finalize() before the force-walk, so a reasoned terminal
    # pause with a warning drops the root warnings summary into the live body.
    events = [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("3", "runner_warning", agent_id="step:design/spec", message="alpha"),
        ev("4", "step_completed", step="spec", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
        ev("5", "workflow_paused", reason="max_loops", interrupt_reason="max_loops"),
    ]
    drv, out = _driver()
    for e in events:
        drv.on_event(e)
    drv.finish()
    body = _body(out)
    assert any("paused — a step kept sending work back" in ln for ln in body)
    assert any("1 warning this run" in ln for ln in body)


def test_mid_session_nested_pause_emits_via_normal_walk() -> None:
    # A pause during an OPEN step nests inside it. A still-open subagent fold in
    # that step would block the DFS walk; the fold-finalize guard clears
    # `running`, so the normal on_event walk (no finish()) reaches the pause.
    drv, out = _driver()
    for e in [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("3", "tool_call", agent_id="a", id="t1", name="sub_agent",
           input={"subagent_name": "reviewer"}),
        ev("4", "workflow_paused", reason="interrupt", interrupt_reason="interrupt"),
    ]:
        drv.on_event(e)
    # No finish(): the normal walk must already have emitted the nested pause.
    assert any("paused (interrupt)" in ln for ln in _body(out))


def test_compose_activity_line_counts_down_when_countdown_total_set() -> None:
    from imp.cli.live_driver import compose_activity_line
    act = nv.Activity(verb="waiting for limit reset", countdown_total=2940.0,
                      start_ts="2026-01-01T00:00:00+00:00")
    line = compose_activity_line(activity=act, frame=0, color=False,
                                 now_iso="2026-01-01T00:11:00+00:00")
    assert line is not None
    # 2940 - 660 = 2280s remaining -> _fmt_hm -> "38m"
    assert "38m left" in line
    assert "~" not in line
    assert "waiting for limit reset" in line
    # the count-up elapsed segment must NOT appear
    assert "11m 0s" not in line


def test_compose_activity_line_countdown_second_granularity_under_a_minute() -> None:
    from imp.cli.live_driver import compose_activity_line
    # Use a hyphen-free verb so the negative-countdown guard (`"-" not in over`)
    # tests the tail alone — a hyphenated verb like "network - waiting to retry"
    # would itself put a "-" on the line and defeat that assertion.
    act = nv.Activity(verb="waiting for limit reset", countdown_total=100.0,
                      start_ts="2026-01-01T00:00:00+00:00")
    # 100 - 92 = 8s remaining -> second granularity, not the old "<1m left" collapse
    line = compose_activity_line(activity=act, frame=0, color=False,
                                 now_iso="2026-01-01T00:01:32+00:00")
    assert "8s left" in line
    assert "<1m left" not in line
    # remaining clamps at zero once the reset moment has passed: "0s left", never negative
    over = compose_activity_line(activity=act, frame=0, color=False,
                                 now_iso="2026-01-01T00:08:20+00:00")
    assert "0s left" in over
    assert "-" not in over


def test_compose_activity_line_countdown_at_exact_minute_boundary() -> None:
    from imp.cli.live_driver import compose_activity_line
    # remaining == 60.0 exactly -> >= 60 guard is True -> "1m left", not "<1m left"
    act = nv.Activity(verb="waiting for limit reset", countdown_total=120.0,
                      start_ts="2026-01-01T00:00:00+00:00")
    line = compose_activity_line(activity=act, frame=0, color=False,
                                 now_iso="2026-01-01T00:01:00+00:00")
    assert "1m left" in line
    assert "~" not in line
    assert "<1m left" not in line


def test_compose_activity_line_unchanged_when_no_countdown() -> None:
    from imp.cli.live_driver import compose_activity_line
    act = nv.Activity(verb="coding", detail="bash pytest",
                      start_ts="2026-01-01T00:00:00+00:00")
    line = compose_activity_line(activity=act, frame=0, color=False,
                                 now_iso="2026-01-01T00:00:14+00:00")
    # existing count-up render is preserved exactly
    assert "14s" in line
    assert "left" not in line


def test_prose_reflows_to_live_width_between_emissions() -> None:
    # Regression: the live driver must read the terminal width at each emission,
    # not freeze it at construction. Otherwise markdown printed AFTER a terminal
    # resize keeps wrapping to the startup width (the status footer already
    # re-reads term_width() every tick, so only the permanent blocks were stale).
    out = _Pipe()
    w = [200]
    drv = LiveDriver(stream=out, color=False, width=lambda: w[0],
                     status=LiveStatus(out))
    first = ("Alpha bravo charlie delta echo foxtrot golf hotel india juliet "
             "kilo lima mike november oscar papa quebec romeo sierra tango")
    second = ("Uniform victor whiskey xray yankee zulu uniform victor whiskey "
              "xray yankee zulu uniform victor whiskey xray yankee zulu foo")
    for e in [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="one", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("3", "agent_message", agent_id="a",
           raw={"content": [{"type": "text", "text": first}]}, usage={}),
    ]:
        drv.on_event(e)
    w[0] = 24  # terminal resized narrower between turns
    for e in [
        ev("4", "step_started", step="two", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("5", "agent_message", agent_id="a",
           raw={"content": [{"type": "text", "text": second}]}, usage={}),
    ]:
        drv.on_event(e)
    # The turn marker "• " leads the first prose line; ignore it when matching.
    first_words = set(first.split()) | {"•"}
    second_words = set(second.split()) | {"•"}
    body = [ln.rstrip() for ln in _body(out) if ln.strip()]
    first_lines = [ln for ln in body if ln.split() and set(ln.split()) <= first_words]
    second_lines = [ln for ln in body if ln.split() and set(ln.split()) <= second_words]
    # First turn printed pre-resize: one wide line. Second turn wraps to 24.
    assert max(len(ln) for ln in first_lines) > 24, first_lines
    assert len(second_lines) >= 4, body            # reflowed, not one wide line
    assert max(len(ln) for ln in second_lines) <= 24, second_lines


# --------------------------------------------------------------------------- #
# compose_position_row                                                        #
# --------------------------------------------------------------------------- #


def _pos_activity(**kw):
    base = dict(phase="implementation phase", plan="plan 1/1", counter="task 1/3",
                model="m", kind="agent")
    base.update(kw)
    return nv.Activity(**base)


def test_compose_position_row_is_flat_and_identity_first() -> None:
    from imp.cli.live_driver import compose_position_row
    row = compose_position_row(activity=_pos_activity(), quest_id="z9gixx",
                               title="streaming footer", prompt_tokens=None,
                               color=False, width=80)
    # One flat left-aligned line, identity first, no right-flush gap.
    assert row == ("z9gixx · streaming footer · implementation phase · "
                   "plan 1/1 · task 1/3 · m")


def test_compose_position_row_omits_title_segment_when_untitled() -> None:
    from imp.cli.live_driver import compose_position_row
    row = compose_position_row(activity=_pos_activity(), quest_id="z9gixx",
                               title="", prompt_tokens=None, color=False, width=80)
    # No title -> no segment and no stray separator between id and phase.
    assert row == ("z9gixx · implementation phase · plan 1/1 · task 1/3 · m")


def test_compose_position_row_starts_with_title_when_quest_id_is_none() -> None:
    from imp.cli.live_driver import compose_position_row
    titled = compose_position_row(activity=_pos_activity(), quest_id=None,
                                  title="streaming footer", prompt_tokens=None,
                                  color=False, width=80)
    # No id -> row starts with the title, no leading separator.
    assert titled == ("streaming footer · implementation phase · plan 1/1 · "
                      "task 1/3 · m")
    untitled = compose_position_row(activity=_pos_activity(), quest_id=None,
                                    title="", prompt_tokens=None, color=False,
                                    width=80)
    # No id and no title -> row starts with the phase.
    assert untitled == "implementation phase · plan 1/1 · task 1/3 · m"


def test_compose_position_row_context_bar_only_for_agent_kind() -> None:
    from imp.cli.live_driver import compose_position_row
    agent = compose_position_row(activity=_pos_activity(kind="agent"), quest_id="z",
                                 title="t", prompt_tokens=None, color=False, width=80,
                                 context_pct=42.0)
    script = compose_position_row(activity=_pos_activity(kind="script"), quest_id="z",
                                  title="t", prompt_tokens=None, color=False, width=80,
                                  context_pct=42.0)
    none_kind = compose_position_row(activity=_pos_activity(kind=None), quest_id="z",
                                     title="t", prompt_tokens=None, color=False, width=80,
                                     context_pct=42.0)
    assert "42%" in agent
    assert "%" not in script
    assert "%" not in none_kind   # prelaunch kind=None suppresses the bar even with usage present
    assert "implementation phase" in script


def test_compose_position_row_context_bar_absent_during_lane_wait() -> None:
    from imp.cli.live_driver import compose_position_row
    row = compose_position_row(activity=_pos_activity(kind="agent", lane_wait=True),
                               quest_id="z", title="t", prompt_tokens=None,
                               color=False, width=80, context_pct=42.0)
    assert "%" not in row
    assert "implementation phase" in row


def test_compose_position_row_colors_match_the_scrollback_palette() -> None:
    from imp.cli.live_driver import compose_position_row
    try:
        pal.set_theme("ansi")
        row = compose_position_row(
            activity=_pos_activity(phase="planning phase", plan="plan 3/6",
                                   counter="task 2/3", model="m", kind="agent"),
            quest_id="z9gixx", title="t", prompt_tokens=None, color=True,
            width=120, context_pct=42.0)
    finally:
        pal.set_theme("ansi")
    parts = row.split(" · ")
    # Flat order: id · title · phase · plan · counter · (model bar).
    assert "\x1b[33m" in parts[0]                                  # id: wordmark gold
    assert "\x1b[" not in parts[1]                                 # title: unpainted default fg
    assert "\x1b[34m" in parts[2] and "\x1b[33m" not in parts[2]   # phase: step-blue
    assert "\x1b[34m" in parts[3]                                  # plan: step-blue
    assert "\x1b[34m" in parts[4]                                  # counter: step-blue
    assert "\x1b[90m" in parts[5] and "\x1b[34m" not in parts[5]   # model: gray, not blue


def test_compose_position_row_leads_with_the_project_segment() -> None:
    from imp.cli.live_driver import compose_position_row
    row = compose_position_row(activity=_pos_activity(), project="imp",
                               quest_id="vope45", title="t", prompt_tokens=None,
                               color=False, width=80)
    # project first; every existing segment keeps its current relative order.
    assert row == ("imp · vope45 · t · implementation phase · plan 1/1 · "
                   "task 1/3 · m")


def test_compose_position_row_without_project_is_byte_for_byte_unchanged() -> None:
    from imp.cli.live_driver import compose_position_row
    omitted = compose_position_row(activity=_pos_activity(), quest_id="vope45",
                                   title="t", prompt_tokens=None, color=False,
                                   width=80)
    empty = compose_position_row(activity=_pos_activity(), project="",
                                 quest_id="vope45", title="t", prompt_tokens=None,
                                 color=False, width=80)
    assert omitted == empty == ("vope45 · t · implementation phase · plan 1/1 · "
                                "task 1/3 · m")
    assert not omitted.startswith(" · ")


def test_compose_position_row_project_is_gray_like_the_model_segment() -> None:
    from imp.cli.live_driver import compose_position_row
    try:
        pal.set_theme("ansi")
        row = compose_position_row(activity=_pos_activity(), project="imp",
                                   quest_id="z9gixx", title="t", prompt_tokens=None,
                                   color=True, width=120)
    finally:
        pal.set_theme("ansi")
    parts = row.split(" · ")
    assert parts[0] == "\x1b[90mimp\x1b[0m"        # project: gray, model's tone
    assert "\x1b[33m" in parts[1]                  # id: still wordmark gold
    assert "\x1b[" not in parts[2]                 # title: still unpainted
    assert "\x1b[34m" in parts[3]                  # phase: still step-blue
    assert "\x1b[90m" in parts[-1]                 # model: the same gray


def test_compose_position_row_bands_and_pads_with_project_under_dark_theme() -> None:
    from imp.cli.live_driver import compose_position_row
    try:
        pal.set_theme("dark")
        row = compose_position_row(activity=_pos_activity(), project="imp",
                                   quest_id="z9gixx", title="t", prompt_tokens=None,
                                   color=True, width=120)
        gray = pal.sgr("gray", enabled=True)
        user_bg = pal.user_bg_open(enabled=True)
        clipped = pal.clip_display(row, 120 - 1)
    finally:
        pal.set_theme("ansi")
    assert row.startswith("\x1b[48;2;43;47;58m")   # cool blue-gray toolbar band survives
    assert row.endswith("\x1b[0m")
    assert user_bg not in row                      # never the amber user-scrollback band
    assert pal.display_width(row) == 118           # still padded to width - 2
    assert clipped == row                          # survives the LiveStatus clip untouched
    # fg-only reset after the segment, so the band keeps painting underneath
    assert f"{gray}imp\x1b[22;39m" in row


def test_compose_prelaunch_position_row_forwards_project() -> None:
    from imp.cli.live_driver import compose_prelaunch_position_row
    row = compose_prelaunch_position_row(
        phase="implementation phase", plan="plan 1/1", step_label="task 1/3",
        model="m", project="imp", quest_id="vope45", title="t",
        color=False, width=80)
    assert row == ("imp · vope45 · t · implementation phase · plan 1/1 · "
                   "task 1/3 · m")


def _sub_builder(*subs, readings=(), start_ts=None):
    """A builder with an agent step and one running sub_agent fold per (event_id, name)."""
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.add(ev("2", "step_started", step="s", kind="agent", phase="p",
             model="m", provider="anthropic"))
    for eid, name in subs:
        b.add(ev(eid, "tool_call", agent_id="step:p/s", id=f"blk-{eid}",
                 name="sub_agent", input={"subagent_name": name},
                 timestamp=start_ts))
    for r in readings:
        b.add(r)
    return b


def test_compose_subagent_rows_one_per_running_subagent() -> None:
    from imp.cli.live_driver import compose_subagent_rows
    b = _sub_builder(("t1", "coder"), ("t2", "explorer"))
    rows = compose_subagent_rows(b, color=False, width=120,
                                 now_iso="2026-01-01T00:00:00+00:00")
    assert len(rows) == 2
    joined = "\n".join(rows)
    assert "coder" in joined      # role still identifies each row
    assert "explorer" in joined
    assert "writing code" not in joined                 # verb segment dropped
    assert "exploring" not in joined


def test_compose_subagent_rows_number_colliding_roles() -> None:
    from imp.cli.live_driver import compose_subagent_rows
    b = _sub_builder(("t1", "explorer"), ("t2", "explorer"))
    rows = compose_subagent_rows(b, color=False, width=120,
                                 now_iso="2026-01-01T00:00:00+00:00")
    joined = "\n".join(rows)
    assert "explorer 1" in joined and "explorer 2" in joined


def test_compose_subagent_rows_carry_own_context_and_elapsed() -> None:
    from imp.cli.live_driver import compose_subagent_rows
    reading = ev("r1", "agent_message", agent_id="sub:t1", provider="anthropic", model="m",
                 stop_reason="end_turn",
                 usage={"input_tokens": 60000, "output_tokens": 5,
                        "cache_read_input_tokens": 20000, "cache_creation_input_tokens": 0},
                 context_pct=42.0, context_window=200_000)
    b = _sub_builder(("t1", "coder"), readings=[reading],
                     start_ts="2026-01-01T00:00:00+00:00")
    rows = compose_subagent_rows(b, color=False, width=120,
                                 now_iso="2026-01-01T00:00:05+00:00")
    assert len(rows) == 1
    assert "42%" in rows[0]     # the subagent's OWN context gauge (Part 2)
    assert "5s" in rows[0]      # its own elapsed (start_ts .. now_iso)


def test_compose_subagent_rows_empty_when_none_running() -> None:
    from imp.cli.live_driver import compose_subagent_rows
    b = _sub_builder()
    assert compose_subagent_rows(b, color=False, width=120,
                                 now_iso="2026-01-01T00:00:00+00:00") == []


def test_compose_subagent_rows_carry_the_slate_and_align_under_dark_theme() -> None:
    from imp.cli import palette as pal
    from imp.cli.live_driver import compose_subagent_rows
    b = _sub_builder(("t1", "coder"))
    try:
        pal.set_theme("dark")
        rows = compose_subagent_rows(b, color=True, width=120,
                                     now_iso="2026-01-01T00:00:00+00:00")
    finally:
        pal.set_theme("ansi")
    assert len(rows) == 1
    assert rows[0].startswith("\x1b[48;2;43;47;58m")   # the position row's cool blue-gray band open
    assert rows[0].endswith("\x1b[0m")
    assert pal.display_width(rows[0]) == 118           # padded to width-2, aligned with parent


def test_compose_subagent_rows_pad_to_the_band_width_with_a_gauge() -> None:
    from imp.cli import palette as pal
    from imp.cli.live_driver import compose_subagent_rows
    reading = ev("r1", "agent_message", agent_id="sub:t1", provider="anthropic",
                 model="m", stop_reason="end_turn",
                 usage={"input_tokens": 60000, "output_tokens": 5},
                 context_pct=42.0, context_window=200_000)
    b = _sub_builder(("t1", "coder"), readings=[reading],
                     start_ts="2026-01-01T00:00:00+00:00")
    try:
        pal.set_theme("dark")
        banded = compose_subagent_rows(b, color=True, width=120,
                                       now_iso="2026-01-01T00:00:05+00:00")
    finally:
        pal.set_theme("ansi")
    assert len(banded) == 1
    assert banded[0].startswith("\x1b[48;2;43;47;58m")   # band survives the wider gauge
    assert banded[0].endswith("\x1b[0m")
    assert pal.display_width(banded[0]) == 118           # still padded to width - 2
    plain = compose_subagent_rows(b, color=False, width=120,
                                  now_iso="2026-01-01T00:00:05+00:00")
    assert plain[0].endswith("████░░░░░░ 42%")           # gauge replaces "context 42%"


def test_compose_subagent_rows_prefix_child_arrow() -> None:
    from imp.cli.live_driver import compose_subagent_rows
    b = _sub_builder(("t1", "coder"))
    rows = compose_subagent_rows(b, color=False, width=120,
                                 now_iso="2026-01-01T00:00:00+00:00")
    assert len(rows) == 1
    # No indent: the row content begins at the child arrow.
    assert rows[0].startswith("↳ ")
    assert rows[0][2:].lstrip().startswith("coder")   # role follows the arrow


def test_compose_subagent_rows_render_plain_without_slate_under_ansi_theme() -> None:
    from imp.cli import palette as pal
    from imp.cli.live_driver import compose_subagent_rows
    b = _sub_builder(("t1", "coder"))
    try:
        pal.set_theme("ansi")
        rows = compose_subagent_rows(b, color=True, width=120,
                                     now_iso="2026-01-01T00:00:00+00:00")
    finally:
        pal.set_theme("ansi")
    assert len(rows) == 1
    assert not rows[0].startswith("\x1b[48")   # 16-color theme has no band background
    assert not rows[0].startswith(" ")         # no leading indent; row begins at the arrow
    assert "↳" in rows[0]                       # child arrow present


def test_compose_subagent_rows_freeze_elapsed_during_backoff_wait() -> None:
    from imp.cli.live_driver import compose_subagent_rows
    b = _sub_builder(("t1", "coder"), start_ts="2026-01-01T00:00:00+00:00")
    # The subagent retries 5s in; its agent_retry routes into the nested builder.
    b.add(ev("r1", "agent_retry", agent_id="sub:t1", category="auth",
             attempt=1, backoff_seconds=30.0,
             timestamp="2026-01-01T00:00:05+00:00"))
    row10 = compose_subagent_rows(b, color=False, width=120,
                                  now_iso="2026-01-01T00:00:10+00:00")[0]
    row20 = compose_subagent_rows(b, color=False, width=120,
                                  now_iso="2026-01-01T00:00:20+00:00")[0]
    assert "· 5s" in row10   # 10s wall - 5s wait (05..10); exact segment, not "15s"
    # As now advances the in-progress wait advances with it, so the honest working
    # time — and thus the whole row — is byte-identical (rules out partial subtraction).
    assert row20 == row10


def test_subagent_backoff_wait_surfaces_to_activity_not_rows() -> None:
    from imp.cli.live_driver import (
        compose_activity_line,
        compose_position_row,
        compose_subagent_rows,
    )
    b = _sub_builder(("t1", "code-reviewer"), start_ts="2026-01-01T00:00:00+00:00")
    b.add(ev("r1", "agent_retry", agent_id="sub:t1", category="auth",
             attempt=1, backoff_seconds=30.0,
             timestamp="2026-01-01T00:00:05+00:00"))
    row = compose_subagent_rows(b, color=False, width=120,
                                now_iso="2026-01-01T00:00:10+00:00")[0]
    # No verb / countdown tag lands on the subagent row.
    assert "left" not in row
    assert "waiting for re-authentication" not in row
    act = b.current_activity()
    # No countdown tag on the position row either.
    pos = compose_position_row(activity=act, quest_id="gizo14", title="t",
                               prompt_tokens=None, color=False, width=120)
    assert "left" not in pos
    # The wait surfaces on the activity line instead.
    line = compose_activity_line(activity=act, frame=0, color=False,
                                 now_iso="2026-01-01T00:00:10+00:00")
    assert "waiting for re-authentication" in line
    assert "25s left" in line          # 30 - 5 = 25s remaining


def test_compose_status_appends_a_row_per_running_subagent() -> None:
    drv, _ = _driver()
    drv.on_event(ev("1", "quest_created", quest_id=1, title="t"))
    drv.on_event(ev("2", "step_started", step="s", kind="agent", phase="p",
                     model="m", provider="anthropic", label="writing spec",
                     phase_label="design phase"))
    drv.on_event(ev("3", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
                     input={"subagent_name": "coder"}))
    block = drv.compose_status(0)
    assert isinstance(block, list) and len(block) == 4   # row1, blank, row3, + one subagent row
    assert "coder" in block[3]              # role present
    assert "writing code" not in block[3]   # verb segment dropped


def test_seed_pending_reply_emits_once_and_reconciles() -> None:
    drv, out = _driver()
    for e in [
        ev("1", "quest_created", quest_id=1, title="t"),
        ev("2", "step_started", step="spec-writer", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        ev("2b", "awaiting_user_input", agent_id="step:spec/spec-writer"),
    ]:
        drv.on_event(e)
    # Optimistic echo fires at submit time — before any round-trip user_message.
    drv.seed_pending_reply("typed reply")
    assert out.getvalue().count("> typed reply") == 1
    # The round-tripped authoritative user_message reconciles into the pending
    # node in place: no second emission.
    drv.on_event(ev("3", "user_message", agent_id="step:spec/spec-writer",
                    content=[{"type": "text", "text": "typed reply"}]))
    assert out.getvalue().count("> typed reply") == 1
    # Exactly one UserMessageNode survives in the tree.
    tree = drv._builder.tree
    phase = next(c for c in tree.root.children if isinstance(c, nv.PhaseNode))
    step = next(c for c in phase.children if isinstance(c, nv.StepNode))
    replies = [c for c in step.children if isinstance(c, nv.UserMessageNode)]
    assert len(replies) == 1


def test_append_committed_reply_emits_echo_without_arming_pending() -> None:
    drv, out = _driver()
    for e in [
        ev("1", "change_created", quest_id=1, title="t"),
        ev("2", "step_started", step="spec-writer", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        ev("2b", "awaiting_user_input", agent_id="step:spec/spec-writer"),
    ]:
        drv.on_event(e)
    drv.append_committed_reply("/abandon")
    # The control-command echo renders once, with the caret, like a normal reply.
    assert out.getvalue().count("> /abandon") == 1
    # It arms NO pending-reply reconciliation (no daemon user_message will arrive).
    assert drv._builder._pending_reply_node is None


def test_seed_pending_reply_lifts_replay_suppression_on_resume() -> None:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out),
                     suppress_replay=True)
    # Replayed prefix (marked _replay), including the gate the user is now at:
    # stays suppressed, nothing printed.
    for e in [
        ev("1", "quest_created", quest_id=1, title="t"),
        ev("2", "step_started", step="spec-writer", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        ev("2b", "awaiting_user_input", agent_id="step:spec/spec-writer"),
    ]:
        e["_replay"] = True
        drv.on_event(e)
    # The boundary freezes the replayed-node snapshot; the prefix stays hidden.
    drv.on_event({"type": "replay_complete"})
    assert out.getvalue() == ""
    # The seeded node is created AFTER the boundary, so the walk lifts
    # suppression at it and emits the echo.
    drv.seed_pending_reply("resume reply")
    assert out.getvalue().count("> resume reply") == 1


def test_append_committed_reply_lifts_replay_suppression_on_resume() -> None:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out),
                     suppress_replay=True)
    # Replayed prefix (marked _replay), including the gate the user is now at:
    # stays suppressed, nothing printed.
    for e in [
        ev("1", "change_created", quest_id=1, title="t"),
        ev("2", "step_started", step="spec-writer", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        ev("2b", "awaiting_user_input", agent_id="step:spec/spec-writer"),
    ]:
        e["_replay"] = True
        drv.on_event(e)
    # The boundary freezes the replayed-node snapshot; the prefix stays hidden.
    drv.on_event({"type": "replay_complete"})
    assert out.getvalue() == ""
    # The committed node is created AFTER the boundary, so the walk lifts
    # suppression at it and emits the echo (mirroring the seeded-pending case).
    drv.append_committed_reply("/abandon")
    assert out.getvalue().count("> /abandon") == 1


def test_seed_pending_reply_lifts_replay_suppression_after_open_burst_tail() -> None:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out),
                     suppress_replay=True)
    # Replayed prefix whose tail is an OPEN tool burst: a tool_call/tool_result
    # with no following prose turn, so _current_burst is still set when the
    # awaiting_user_input gate is replayed. All suppressed — nothing printed.
    for e in [
        ev("1", "quest_created", quest_id=1, title="t"),
        ev("2", "step_started", step="spec-writer", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        ev("3", "tool_call", agent_id="step:spec/spec-writer", id="b1",
           name="read_file", input={"p": "1"}),
        ev("4", "tool_result", agent_id="step:spec/spec-writer", id="b1",
           content="ok"),
        ev("2b", "awaiting_user_input", agent_id="step:spec/spec-writer"),
    ]:
        e["_replay"] = True
        drv.on_event(e)
    # The boundary freezes the replayed-node snapshot; the prefix stays hidden.
    drv.on_event({"type": "replay_complete"})
    assert out.getvalue() == ""
    # The seeded node is created AFTER the boundary. The gate must have flushed
    # the open burst so the walk can reach the reply node and emit its echo.
    drv.seed_pending_reply("resume reply")
    assert out.getvalue().count("> resume reply") == 1


def test_append_committed_reply_lifts_replay_suppression_after_open_burst_tail() -> None:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out),
                     suppress_replay=True)
    # Replayed prefix whose tail is an OPEN tool burst (see the seeded-pending
    # sibling). All suppressed — nothing printed until the post-boundary reply.
    for e in [
        ev("1", "change_created", quest_id=1, title="t"),
        ev("2", "step_started", step="spec-writer", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        ev("3", "tool_call", agent_id="step:spec/spec-writer", id="b1",
           name="read_file", input={"p": "1"}),
        ev("4", "tool_result", agent_id="step:spec/spec-writer", id="b1",
           content="ok"),
        ev("2b", "awaiting_user_input", agent_id="step:spec/spec-writer"),
    ]:
        e["_replay"] = True
        drv.on_event(e)
    # The boundary freezes the replayed-node snapshot; the prefix stays hidden.
    drv.on_event({"type": "replay_complete"})
    assert out.getvalue() == ""
    # The committed node is created AFTER the boundary; the gate must have
    # flushed the open burst so the walk reaches it and emits the echo once.
    drv.append_committed_reply("/abandon")
    assert out.getvalue().count("> /abandon") == 1


def test_reset_wait_does_not_re_emit_running_step_header() -> None:
    # A running non-position_label agent step interrupted by a hinted rate-limit
    # wait keeps its single header; the wait banner must not print it a 2nd time.
    drv, out = _driver()
    for e in [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="reviewer", kind="agent",
           phase="integration", phase_label="integration phase",
           label="reviewing merge conflicts", model="m", provider="anthropic"),
        ev("3", "agent_retry", agent_id="step:integration/reviewer",
           category="rate_limit", attempt=1, backoff_seconds=2940.0, hinted=True),
    ]:
        drv.on_event(e)
    body = _body(out)
    header_hits = [ln for ln in body if "reviewing merge conflicts" in ln]
    assert len(header_hits) == 1


def test_body_less_deferred_leaf_emits_no_separate_header() -> None:
    # A visible body-less step (acquire, has a label, no command/agent) collapses
    # to a single `<phase> · <label> · complete` leaf; the mid-step position
    # header must NOT also print while it is the open top-of-stack.
    drv, out = _driver()
    events = [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="wait-for-head", kind="acquire",
           phase="integration", phase_label="integration phase",
           label="waiting in merge queue"),
        ev("3", "step_completed", step="wait-for-head", kind="acquire",
           action={"verb": "continue"}, var_mutations={}),
    ]
    for e in events:
        drv.on_event(e)
    drv.finish()
    body = _body(out)
    hits = [ln for ln in body if "waiting in merge queue" in ln]
    assert hits == ["integration phase · waiting in merge queue · complete"]
    assert body == _render_body(events)   # live == imp log (single collapsed leaf)


def test_seed_opening_intent_emits_at_root_before_any_event() -> None:
    drv, out = _driver()
    # Optimistic echo fires at submit time — before any step_started / opening_intent
    # event arrives from the daemon.
    drv.seed_opening_intent("build the parser")
    assert out.getvalue().count("build the parser") == 1
    # It lives at root (no step exists yet), rendered as an interactive user turn.
    tree = drv._builder.tree
    intents = [c for c in tree.root.children if isinstance(c, nv.UserMessageNode)]
    assert len(intents) == 1
    assert intents[0].text == "build the parser"
    assert intents[0].interactive is True


def test_seed_opening_intent_reconciles_with_daemon_event_no_duplicate() -> None:
    drv, out = _driver()
    drv.seed_opening_intent("build the parser")
    assert out.getvalue().count("build the parser") == 1
    # The daemon's authoritative opening_intent event adopts the seeded node in
    # place: no second line, no second node.
    drv.on_event(ev("e1", "user_message", role="user", subtype="intent",
                    content=[{"type": "text", "text": "build the parser"}]))
    assert out.getvalue().count("build the parser") == 1
    # And the first step_started does not re-home it — still exactly one at root.
    drv.on_event(ev("3", "step_started", step="spec-writer", kind="agent",
                    phase="spec", model="m", provider="anthropic"))
    tree = drv._builder.tree
    intents = [c for c in tree.root.children if isinstance(c, nv.UserMessageNode)]
    assert len(intents) == 1
    phase = next(c for c in tree.root.children if isinstance(c, nv.PhaseNode))
    step = next(c for c in phase.children if isinstance(c, nv.StepNode))
    assert not any(isinstance(c, nv.UserMessageNode) for c in step.children)


def test_compose_startup_returns_none_without_verb() -> None:
    drv, _out = _driver()                          # no startup_verb
    assert drv.compose_startup(0) is None


def test_startup_verb_none_install_is_noop() -> None:
    out = _TTY()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    drv.install_startup_status()
    assert out.getvalue() == ""                    # imp new / chat path: nothing drawn


def test_install_startup_status_inert_on_non_tty() -> None:
    out = _Pipe()                                  # not a tty
    status = LiveStatus(out)
    drv = LiveDriver(stream=out, color=False, width=80, status=status,
                     startup_verb="resuming")
    drv.install_startup_status()
    assert out.getvalue() == ""                    # LiveStatus._enabled is False


def test_install_startup_status_draws_and_animates_resuming() -> None:
    out = _TTY()
    status = LiveStatus(out)
    status._width_fn = lambda: 80
    drv = LiveDriver(stream=out, color=False, width=80, status=status,
                     startup_verb="resuming")
    drv.install_startup_status()
    assert "resuming" in out.getvalue()
    assert "0s" in out.getvalue()                  # elapsed starts at ~0 at install
    # Block-age timer: backdate the startup block start and the line counts up.
    from datetime import UTC, datetime, timedelta
    drv._startup_start_iso = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    out.truncate(0)
    out.seek(0)
    status._frame += 1
    status._draw()                                 # one spinner tick
    assert "resuming" in out.getvalue()            # still shown
    assert "5s" in out.getvalue()                  # elapsed advanced via block-age


def test_startup_line_persists_through_replay_then_replaced_at_boundary() -> None:
    out = _TTY()
    status = LiveStatus(out)
    status._width_fn = lambda: 80
    drv = LiveDriver(stream=out, color=False, width=80, status=status,
                     suppress_replay=True, startup_verb="resuming")
    drv.install_startup_status()
    events = _two_phase_events()
    marked = [{**e, "_replay": True} for e in events[:3]] + events[3:]
    for e in marked[:3]:                           # replayed prefix only
        drv.on_event(e)
    # Bound methods compare EQUAL (==) for the same instance+function, but each
    # attribute access yields a fresh object, so `is` is always False — use `==`.
    assert status._compose == drv.compose_startup   # startup line NOT swapped during replay
    assert "resuming" in out.getvalue()
    drv.on_event({"type": "replay_complete"})      # boundary lights the footer
    assert status._compose == drv.compose_status    # replaced by the real footer
    for e in marked[3:]:                            # first live event onward
        drv.on_event(e)
    assert status._compose == drv.compose_status    # stays the real footer


def test_seed_opening_intent_arms_preboot_spinner(monkeypatch) -> None:
    monkeypatch.setattr("imp.agents.random_status.random_status_phrase",
                        lambda: "scheming")
    drv, _out = _driver()
    # Before submit: no step/phase, footer dark.
    assert drv._builder.current_activity() is None
    drv.seed_opening_intent("build the parser")
    # The opening echo now also arms the pre-boot spinner, so the footer draws
    # `⠋ scheming` immediately — before any daemon event arrives.
    act = drv._builder.current_activity()
    assert act is not None
    assert act.verb == "scheming"


def test_activity_line_matches_subagent_row_for_single_subagent() -> None:
    # The reported bug: activity line "1m 44s" beside "↳ … · 35s". With one
    # running non-waiting subagent the two tails must be identical for one frame.
    from imp.cli import narrative as nv
    from imp.cli.live_driver import compose_activity_line, compose_subagent_rows
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.add(ev("2", "step_started", step="s", kind="agent", phase="p",
             model="m", provider="anthropic"))
    b.add(ev("t1", "tool_call", agent_id="step:p/s", id="blk-t1", name="sub_agent",
             input={"subagent_name": "code-reviewer"},
             timestamp="2026-01-01T00:00:00+00:00"))
    now = "2026-01-01T00:01:44+00:00"          # 104s later
    act = b.current_activity()
    line = compose_activity_line(activity=act, frame=0, color=False, now_iso=now)
    row = compose_subagent_rows(b, color=False, width=120, now_iso=now)[0]
    fold = b.running_subagents()[0]
    from imp.cli import render_text as rt
    expected = rt.fmt_seconds(nv.elapsed_seconds(fold.start_ts, now)
                              - fold.nested.backoff_wait_seconds(now))
    assert f"· {expected}" in line and f"· {expected}" in row


def test_activity_line_derives_countdown_from_block_start() -> None:
    from imp.cli import narrative as nv
    from imp.cli.live_driver import compose_activity_line
    act = nv.Activity(verb="waiting for limit reset", countdown_total=2940.0,
                      start_ts="2026-01-01T00:00:00+00:00")
    line = compose_activity_line(activity=act, frame=0, color=False,
                                 now_iso="2026-01-01T00:11:00+00:00")   # 660s in
    assert "38m left" in line                  # 2940 - 660 = 2280s -> 38m
    assert "11m 0s" not in line                # no count-up leak


def test_compose_status_takes_one_arg_and_returns_footer_rows() -> None:
    drv, _ = _driver()
    drv.on_event(ev("1", "quest_created", quest_id=1, title="t"))
    drv.on_event(ev("2", "step_started", step="s", kind="agent", phase="p",
                    model="m", provider="anthropic", label="writing spec",
                    phase_label="design phase"))
    block = drv.compose_status(0)              # one arg only
    assert isinstance(block, list) and len(block) == 3   # activity, blank, position


def test_driver_seeds_identity_at_construction() -> None:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out),
                     quest_id="lufu50", title="Instant opening identity bar")
    root = drv._builder.tree.root
    assert root.id == "lufu50"                     # set before any event
    assert root.title == "Instant opening identity bar"


def test_driver_without_quest_id_leaves_root_unseeded() -> None:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    assert drv._builder.tree.root.id is None       # default: no identity yet


def test_compose_startup_appends_identity_row_when_seeded() -> None:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out),
                     startup_verb="resuming",
                     quest_id="lufu50", title="Instant opening identity bar")
    rows = drv.compose_startup(0)
    assert rows is not None
    assert len(rows) == 3                          # spinner / blank / identity
    assert "resuming" in rows[0]
    assert rows[1] == ""
    assert "lufu50" in rows[2]
    assert "Instant opening identity bar" in rows[2]


def test_compose_startup_spinner_only_when_id_absent() -> None:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out),
                     startup_verb="resuming")        # no quest_id seeded
    rows = drv.compose_startup(0)
    assert rows is not None
    assert len(rows) == 1                            # degrades to spinner-only
    assert "resuming" in rows[0]


def test_compose_position_row_shows_unsandboxed_after_title() -> None:
    from imp.cli.live_driver import compose_position_row
    row = compose_position_row(activity=_pos_activity(), quest_id="z9gixx",
                               title="streaming footer", prompt_tokens=None,
                               color=False, width=80, unsandboxed=True)
    assert row == ("z9gixx · streaming footer · unsandboxed · implementation "
                   "phase · plan 1/1 · task 1/3 · m")


def test_compose_position_row_unsandboxed_after_id_when_no_title() -> None:
    from imp.cli.live_driver import compose_position_row
    row = compose_position_row(activity=_pos_activity(), quest_id="z9gixx",
                               title="", prompt_tokens=None,
                               color=False, width=80, unsandboxed=True)
    assert row == ("z9gixx · unsandboxed · implementation "
                   "phase · plan 1/1 · task 1/3 · m")


def test_compose_identity_toolbar_unsandboxed_after_title() -> None:
    from imp.cli.live_driver import compose_identity_toolbar
    bar = compose_identity_toolbar(quest_id="z9gixx", title="t", color=False,
                                   width=80, unsandboxed=True)
    assert bar == "z9gixx · t · unsandboxed"


def test_compose_identity_toolbar_leads_with_the_project_segment() -> None:
    from imp.cli.live_driver import compose_identity_toolbar
    bar = compose_identity_toolbar(project="imp", quest_id="vope45", title="t",
                                   color=False, width=100)
    assert bar == "imp · vope45 · t"
    unsandboxed_bar = compose_identity_toolbar(project="imp", quest_id="vope45", title="t",
                                               color=False, width=100, unsandboxed=True)
    assert unsandboxed_bar == "imp · vope45 · t · unsandboxed"


def test_compose_identity_toolbar_without_project_is_byte_for_byte_unchanged() -> None:
    from imp.cli.live_driver import compose_identity_toolbar
    omitted = compose_identity_toolbar(quest_id="vope45", title="t",
                                       color=False, width=100)
    empty = compose_identity_toolbar(project="", quest_id="vope45", title="t",
                                     color=False, width=100)
    assert omitted == empty == "vope45 · t"      # no leading " · "
    # A title with no id still bands; nothing at all -> None.
    assert compose_identity_toolbar(project="", quest_id=None, title="imp setup",
                                    color=False, width=80) == "imp setup"
    assert compose_identity_toolbar(project="", quest_id=None, title="",
                                    color=False, width=80) is None


def test_compose_identity_toolbar_unsandboxed_after_id_when_no_title() -> None:
    from imp.cli.live_driver import compose_identity_toolbar
    bar = compose_identity_toolbar(quest_id="z9gixx", title="", color=False,
                                   width=80, unsandboxed=True)
    assert bar == "z9gixx · unsandboxed"


def test_compose_position_row_unsandboxed_is_bold_red_under_ansi() -> None:
    from imp.cli.live_driver import compose_position_row
    try:
        pal.set_theme("ansi")
        row = compose_position_row(activity=_pos_activity(), quest_id="z9gixx",
                                   title="t", prompt_tokens=None, color=True,
                                   width=120, unsandboxed=True)
    finally:
        pal.set_theme("ansi")
    parts = row.split(" · ")
    # id · title · unsandboxed · phase · ...
    assert "unsandboxed" in parts[2]
    assert "\x1b[1m" in parts[2] and "\x1b[31m" in parts[2]


def test_compose_prelaunch_position_row_renders_state_segments_no_context_bar() -> None:
    from imp.cli.live_driver import compose_prelaunch_position_row
    row = compose_prelaunch_position_row(
        phase="planning phase", plan="plan 2/4", step_label="reviewing spec",
        model="sonnet", quest_id="feat", title="demo",
        color=False, width=200)
    assert row == "feat · demo · planning phase · plan 2/4 · reviewing spec · sonnet"
    assert "%" not in row      # context-usage bar omitted pre-launch


def test_compose_prelaunch_position_row_omits_absent_segments() -> None:
    from imp.cli.live_driver import compose_prelaunch_position_row
    row = compose_prelaunch_position_row(
        phase=None, plan=None, step_label=None, model=None,
        quest_id="feat", title="demo", color=False, width=200)
    assert row == "feat · demo"      # residual: less state → fewer segments, no drift


def test_driver_unsandboxed_flag_defaults_off_and_toggles_into_toolbar() -> None:
    drv = LiveDriver(stream=io.StringIO(), color=False, width=80,
                     status=LiveStatus(io.StringIO()), quest_id="z9gixx",
                     title="t")
    assert drv.unsandboxed is False
    assert "unsandboxed" not in (drv.compose_toolbar_static() or "")
    drv.set_unsandboxed(True)
    assert drv.unsandboxed is True
    assert "unsandboxed" in (drv.compose_toolbar_static() or "")


def test_seed_pending_reply_reaches_reply_past_open_subagent_fold() -> None:
    # Ctrl-C interrupting a running subagent lands on the inline steer prompt via
    # awaiting_user_input. The interrupted subagent never emitted a tool_result,
    # so its SubagentFold stays running=True and sits ahead of the freshly-seeded
    # reply node in walk order. Unless the solicit boundary settles the open
    # fold, the live _walk stalls on it (_is_final is False while running) and the
    # optimistic echo is never painted. Drive events ONLY up to
    # awaiting_user_input, and do NOT call finish() (which would force-flush and
    # mask the stall).
    drv, out = _driver()
    for e in [
        ev("1", "quest_created", quest_id=1, title="t"),
        ev("2", "step_started", step="task-lead", kind="agent", phase="build",
           model="m", provider="anthropic"),
        ev("3", "tool_call", agent_id="step:build/task-lead", id="t1",
           name="sub_agent", id_ns="block", input={"subagent_name": "coder"}),
        # No tool_result for the sub_agent call: the interrupted subagent never
        # emitted one, so its SubagentFold stays running=True. id_ns="block"
        # (awaits_result=True) prevents _resume_close_folds from closing the
        # fold before _h_awaiting_user_input runs, which is the actual scenario.
        ev("4", "awaiting_user_input", agent_id="step:build/task-lead"),
    ]:
        drv.on_event(e)
    drv.seed_pending_reply("steer this way")
    body = out.getvalue()
    assert body.count("> steer this way") == 1, (
        "optimistic steer echo not painted — the live cursor is wedged behind "
        f"the open (running) subagent fold; body={body!r}")
    # Walk order preserved: the interrupted subagent status row renders before
    # the user's echoed reply.
    lines = body.splitlines()
    sub_i = next(i for i, ln in enumerate(lines) if "interrupted" in ln)
    reply_i = next(i for i, ln in enumerate(lines) if "> steer this way" in ln)
    assert sub_i < reply_i, (
        f"interrupted subagent row must precede the echoed reply; body={body!r}")


def test_compose_activity_line_shows_retry_hint_when_total_at_least_60() -> None:
    from imp.cli.live_driver import compose_activity_line
    act = nv.Activity(verb="waiting for limit reset", countdown_total=2940.0,
                      start_ts="2026-01-01T00:00:00+00:00")
    line = compose_activity_line(activity=act, frame=0, color=False,
                                 now_iso="2026-01-01T00:11:00+00:00")
    # the countdown timer is the trailing ` · `-joined segment, after the hint
    assert line.endswith("38m left")
    assert " · 38m left" in line
    assert line.index("press r to retry now") < line.index("38m left")


def test_compose_activity_line_retry_hint_survives_final_minute() -> None:
    from imp.cli.live_driver import compose_activity_line
    # total 120s (>= 60) but only 20s remaining -> gated on total, so hint stays
    act = nv.Activity(verb="waiting for limit reset", countdown_total=120.0,
                      start_ts="2026-01-01T00:00:00+00:00")
    line = compose_activity_line(activity=act, frame=0, color=False,
                                 now_iso="2026-01-01T00:01:40+00:00")
    assert line.endswith("20s left")
    assert line.index("press r to retry now") < line.index("20s left")


def test_compose_activity_line_no_retry_hint_below_60() -> None:
    from imp.cli.live_driver import compose_activity_line
    act = nv.Activity(verb="waiting for limit reset", countdown_total=30.0,
                      start_ts="2026-01-01T00:00:00+00:00")
    line = compose_activity_line(activity=act, frame=0, color=False,
                                 now_iso="2026-01-01T00:00:05+00:00")
    assert "press r to retry now" not in line


def test_echo_submitted_reply_shows_before_replay_and_reconciles_once() -> None:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out),
                     suppress_replay=True)
    # Submit-time echo: rendered NOW, before any replay boundary (pre-spinner).
    drv.echo_submitted_reply("my answer")
    assert out.getvalue().count("> my answer") == 1
    # Replay the parked history — including a PRIOR human turn — then the boundary.
    for e in [
        ev("1", "quest_created", quest_id=1, title="t"),
        ev("2", "step_started", step="planner", kind="agent", phase="plan",
           model="m", provider="anthropic"),
        ev("3", "user_message", agent_id="step:plan/planner", role="user",
           content=[{"type": "text", "text": "an earlier turn"}]),
        ev("3b", "awaiting_user_input", agent_id="step:plan/planner"),
    ]:
        e["_replay"] = True
        drv.on_event(e)
    drv.on_event({"type": "replay_complete"})
    # Replayed history stays hidden; the echo is neither duplicated nor consumed.
    assert out.getvalue().count("> my answer") == 1
    assert "an earlier turn" not in out.getvalue()
    # The daemon's LIVE user_message for the submitted line reconciles in place —
    # no second echo.
    drv.on_event(ev("4", "user_message", agent_id="step:plan/planner", role="user",
                    content=[{"type": "text", "text": "my answer"}]))
    assert out.getvalue().count("> my answer") == 1


def test_compose_activity_line_no_retry_hint_without_countdown() -> None:
    from imp.cli.live_driver import compose_activity_line
    act = nv.Activity(verb="coding", detail="bash pytest",
                      start_ts="2026-01-01T00:00:00+00:00")
    line = compose_activity_line(activity=act, frame=0, color=False,
                                 now_iso="2026-01-01T00:00:14+00:00")
    assert "press r to retry now" not in line


def test_compose_activity_line_retry_hint_at_exactly_60() -> None:
    from imp.cli.live_driver import compose_activity_line
    act = nv.Activity(verb="waiting for limit reset", countdown_total=60.0,
                      start_ts="2026-01-01T00:00:00+00:00")
    line = compose_activity_line(activity=act, frame=0, color=False,
                                 now_iso="2026-01-01T00:00:05+00:00")
    assert line.endswith("55s left")
    assert line.index("press r to retry now") < line.index("55s left")


def test_frame_suppressed_opening_live_log_parity() -> None:
    # Direct AC assertion: the opening frame-suppressed threshold phase+step renders
    # byte-for-byte identically in the live viewer and in `imp log` (render_tree).
    # Pre-fix the batch walk stacks three blanks while the live path already shows
    # one, so this parity fails until the render_text guards land.
    events = [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "user_message", subtype="intent",
           content=[{"type": "text", "text": "do the thing"}]),
        ev("3", "step_started", step="threshold", kind="agent", phase="threshold",
           model="m", provider="anthropic", label="chatting",
           phase_label="threshold phase", show_label=False, phase_show_label=False),
        ev("4", "agent_message", agent_id="step:threshold/threshold",
           provider="anthropic", model="m", stop_reason="end_turn", usage={},
           raw={"content": [{"type": "text", "text": "I can help with that"}]}),
        ev("5", "step_completed", step="threshold", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ]
    drv, out = _driver()
    for e in events:
        drv.on_event(e)
    drv.finish()
    assert _body(out) == _render_body(events)   # live == imp log, byte-for-byte


def test_breadcrumb_to_completion_block_has_exactly_one_blank_end_to_end(
    monkeypatch,
) -> None:
    """AC5 end-to-end: the live breadcrumb, the shared teardown, and the printed exit
    block share one terminal; after the fix exactly one blank row separates them."""
    import contextlib

    import pyte

    from imp.cli import main
    from impty.screen import TerminalScreen
    from impty.view import lines as screen_lines

    class _Quest:
        id = "2026-06-12-run-x"
        title = ""
        base_branch = "master"

    events = [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("3", "step_started", step="merging", kind="agent", phase="integration",
           model="m", provider="anthropic"),
        ev("4", "agent_message", agent_id="a",
           raw={"content": [{"type": "text", "text": "done"}]}, usage={}),
        ev("5", "step_completed", step="merging", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ]
    out = _TTYPipe()                       # isatty() is True → footer draws and owns a blank
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    for e in events:
        drv.on_event(e)
    drv.finish()                           # shared teardown: preserves the one owned blank
    monkeypatch.setenv("NO_COLOR", "1")    # plain block → clean screen rows
    with contextlib.redirect_stdout(out):
        main._emit_completed(_Quest(), merged=True)

    screen = TerminalScreen(80, 16, history=200, ratio=0.5)
    pyte.ByteStream(screen).feed(out.getvalue().encode())
    rows = screen_lines(screen)
    c = next(i for i, r in enumerate(rows) if r.strip() == "merged into master")
    assert rows[c - 1].strip() == ""            # exactly one blank directly above the block
    assert rows[c - 2].strip() != ""            # …and not two — the row above the blank has content
    assert "merging · complete" in rows[c - 2]  # breadcrumb, directly above the blank


def test_finish_exit_block_false_resets_without_trailing_blank() -> None:
    """finish(exit_block=False) uses reset(): no descend past the footer's owned blank."""
    out = _TTYPipe()
    status = LiveStatus(out)
    status._width_fn = lambda: 80
    drv = LiveDriver(stream=out, color=False, width=80, status=status)
    drv.on_event(ev("1", "quest_created", quest_id=1, title="x"))
    drv.on_event(ev("2", "step_started", step="s", kind="agent", phase="spec",
                    model="m", provider="anthropic"))
    assert status._visible                 # footer is visible — sanity check
    out.truncate(0)
    out.seek(0)
    drv.finish(exit_block=False)
    v = out.getvalue()
    assert not v.endswith("\n")            # reset(): no trailing descend past the owned blank
    assert status._compose is None         # compose cleared


def test_finish_default_descends_past_owned_blank() -> None:
    """finish() with default exit_block=True uses end_run(): descends past the owned blank."""
    out = _TTYPipe()
    status = LiveStatus(out)
    status._width_fn = lambda: 80
    drv = LiveDriver(stream=out, color=False, width=80, status=status)
    drv.on_event(ev("1", "quest_created", quest_id=1, title="x"))
    drv.on_event(ev("2", "step_started", step="s", kind="agent", phase="spec",
                    model="m", provider="anthropic"))
    assert status._visible
    out.truncate(0)
    out.seek(0)
    drv.finish()
    v = out.getvalue()
    assert v.endswith("\n")               # end_run(): descend past the owned blank
    assert status._compose is None


def test_resume_echo_keeps_blank_before_first_live_agent_prose() -> None:
    # Inert-resume: the boot echoes the user's typed reply to screen BEFORE the
    # suppressed replay, arming the trailing-blank flag (_prev_prose=True,
    # _emitted_any=True). Crossing into the first live agent node must preserve
    # that blank so the echoed "> ..." line and the agent's first prose line are
    # spaced like every other conversational turn (exactly one blank between).
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out),
                     suppress_replay=True)
    # Submit-time echo: rendered NOW, before any replay boundary.
    drv.echo_submitted_reply("my answer")
    # Replay the parked history (suppressed), then the boundary.
    for e in [
        ev("1", "quest_created", quest_id=1, title="t"),
        ev("2", "step_started", step="planner", kind="agent", phase="plan",
           model="m", provider="anthropic"),
        ev("2b", "awaiting_user_input", agent_id="step:plan/planner"),
    ]:
        e["_replay"] = True
        drv.on_event(e)
    drv.on_event({"type": "replay_complete"})
    # Live turn: the daemon's user_message reconciles the echo in place, then the
    # agent answers. Prose is held until step_completed (F3 dedup hold).
    drv.on_event(ev("3", "user_message", agent_id="step:plan/planner", role="user",
                    content=[{"type": "text", "text": "my answer"}]))
    drv.on_event(ev("4", "agent_message", agent_id="step:plan/planner",
                    raw={"content": [{"type": "text", "text": "working on it"}]}, usage={}))
    drv.on_event(ev("5", "step_completed", step="planner", kind="agent",
                    action={"verb": "continue"}, var_mutations={}))
    drv.finish()
    lines = out.getvalue().split("\n")
    echo_idx = next(i for i, ln in enumerate(lines) if ln == "> my answer")
    prose_idx = next(i for i, ln in enumerate(lines) if "working on it" in ln)
    # Exactly one blank line separates the echoed message from the agent prose.
    assert prose_idx == echo_idx + 2
    assert lines[echo_idx + 1] == ""


def _tty_driver():
    out = _TTY()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    return drv, out


def test_show_stop_ack_renders_ack_text_on_the_status_line() -> None:
    """LiveDriver exposes a public show_stop_ack that renders the transient stop
    acknowledgment on the live status line (delegating to LiveStatus)."""
    drv, out = _tty_driver()
    drv.show_stop_ack("stopping…")
    assert "stopping…" in out.getvalue()


def test_ack_hook_wired_to_driver_renders_stopping_then_force_quit() -> None:
    """Registering driver.show_stop_ack as the coordinator ack hook renders
    'stopping…' on the first gesture and the force-quit upgrade on the second."""
    drv, out = _tty_driver()
    coord = InterruptCoordinator()
    coord.set_ack_hook(drv.show_stop_ack)

    asyncio.run(coord.signal_pause(reason="ctrl-c"))
    assert "stopping…" in out.getvalue()
    assert "interrupt again to force-quit" not in out.getvalue()

    coord._note_gesture_ack(force_quit_available=True)
    assert "stopping… (interrupt again to force-quit)" in out.getvalue()


def test_stop_ack_cleared_when_prompt_gate_lands() -> None:
    """AC5: the stop-ack clears when the interrupt steer gate appears —
    prepare_for_prompt hides the status line, erasing the drawn ack."""
    drv, out = _tty_driver()
    drv.show_stop_ack("stopping…")
    assert drv._status._visible is True
    out.truncate(0)
    out.seek(0)
    drv.prepare_for_prompt()          # the interrupt steer gate lands
    assert drv._status._visible is False       # ack no longer owns the status line
    assert "\x1b[K" in out.getvalue()          # the ack row was erased


def test_driver_titled_without_a_quest_id_shows_the_identity_band() -> None:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out),
                     title="imp setup")
    # No quest_created event will ever arrive during setup, so the title is the
    # whole identity — and it must reach the static toolbar under the field.
    assert drv.compose_toolbar_static() == "imp setup"


def test_finish_clears_the_stop_ack_so_it_cannot_outlive_the_run() -> None:
    """The ack is sticky, so teardown must end it explicitly — otherwise a tick landing
    after finish() repaints `stopping…` onto the exit block."""
    out = _TTYPipe()
    status = LiveStatus(out)
    status._width_fn = lambda: 80
    drv = LiveDriver(stream=out, color=False, width=80, status=status)
    drv.on_event(ev("1", "quest_created", quest_id=1, title="x"))
    drv.on_event(ev("2", "step_started", step="s", kind="agent", phase="spec",
                    model="m", provider="anthropic"))
    drv.show_stop_ack("stopping…")
    drv.finish()
    assert status._stop_ack is None
    out.truncate(0)
    out.seek(0)
    status._draw()                       # a stray spinner tick after teardown
    assert out.getvalue() == ""


def test_full_cats_expands_subagent_fold_like_log() -> None:
    # With the `subagents` category on, the live driver must render the expanded
    # `▾ {name}` fold + recursed inner transcript, byte-identical to imp log --full.
    events = [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="a", id="t1", name="sub_agent",
           input={"subagent_name": "reviewer", "task_description": "review the diff"}),
        ev("5", "thinking", agent_id="sub:4", content="weighing it up"),
        ev("6", "agent_message", agent_id="sub:4",
           raw={"content": [{"type": "text", "text": "the finding"}]}, usage={}),
        ev("7", "tool_result", agent_id="a", id="t1",
           content='{"status":"Success","summary":"ok"}'),
        ev("8", "step_completed", step="spec", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ]
    full = _cats_body(events, frozenset(LOG_CATEGORIES))
    assert any(ln.strip().startswith("▾ reviewer") for ln in full)
    assert any("the finding" in ln for ln in full)     # inner transcript recursed
    assert full == _render_body(events, frozenset(LOG_CATEGORIES))
    lean = _cats_body(events, frozenset())
    assert not any("▾ reviewer" in ln for ln in lean)  # default stays the status row
    assert not any("the finding" in ln for ln in lean)
    assert lean == _render_body(events)


def test_full_cats_emits_complete_step_block_at_step_close() -> None:
    # The stashed complete_step args surface under --full, flush against the step's
    # completion breadcrumb, with the following block still getting its blank above.
    events = [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="rev", kind="agent", phase="review",
           model="m", provider="anthropic"),
        ev("3", "agent_message", agent_id="a",
           raw={"content": [{"type": "text", "text": "reviewed it"}]}, usage={}),
        ev("4", "agent_result", agent_id="step:review/rev", verdict="rework",
           findings="Line 1 is wrong.\nLine 2 needs a guard."),
        ev("5", "step_completed", step="rev", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
        ev("6", "step_started", step="patch", kind="agent", phase="review",
           model="m", provider="anthropic"),
        ev("7", "step_completed", step="patch", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ]
    lean = _cats_body(events, frozenset())
    assert not any("complete_step" in ln for ln in lean)   # hidden by default
    full = _cats_body(events, frozenset(LOG_CATEGORIES))
    assert any("▾ complete_step" in ln for ln in full)
    i = next(k for k, ln in enumerate(full) if "▾ complete_step" in ln)
    assert "Line 2 needs a guard." in "\n".join(full[i:])
    assert "rev · complete" in full[i - 1]      # flush against the breadcrumb
    j = next(k for k, ln in enumerate(full) if "patch" in ln and k > i)
    assert full[j - 1].strip() == ""            # next block keeps its blank above
    assert full == _render_body(events, frozenset(LOG_CATEGORIES))


def test_full_cats_complete_step_keeps_blank_above_a_frameless_step() -> None:
    # A `show_label: false` agent step emits NO lines of its own (header suppressed,
    # breadcrumb skipped), so its complete_step block is the whole rendered block and
    # keeps the blank line above it that render_tree yields. Clearing the driver's
    # armed separator unconditionally at the pop site would eat that blank.
    events = [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="a", kind="agent", phase="p",
           model="m", provider="anthropic"),
        ev("3", "agent_message", agent_id="a",
           raw={"content": [{"type": "text", "text": "first"}]}, usage={}),
        ev("4", "step_completed", step="a", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
        ev("5", "step_started", step="quiet", kind="agent", phase="p",
           model="m", provider="anthropic", show_label=False),
        ev("6", "agent_result", agent_id="step:p/quiet", findings="X" * 100),
        ev("7", "step_completed", step="quiet", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ]
    full = _cats_body(events, frozenset(LOG_CATEGORIES))
    i = next(k for k, ln in enumerate(full) if "▾ complete_step" in ln)
    assert full[i - 1].strip() == ""            # blank above, as render_tree yields
    assert "a · complete" in full[i - 2]
    assert full == _render_body(events, frozenset(LOG_CATEGORIES))


def test_driver_project_leads_the_static_toolbar_and_the_footer_row() -> None:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out),
                     project="imp")
    drv.on_event(ev("1", "quest_created", id="z9gixx", title="t"))
    # (a) compose_toolbar_static before any activity -> the identity band
    assert drv.compose_toolbar_static() == "imp · z9gixx · t"
    drv.on_event(ev("2", "step_started", step="task-lead", kind="agent", phase="code",
                    model="claude-sonnet-4-5", provider="anthropic", label="task 1/3",
                    phase_label="implementation phase · plan 1/1", position_label=True))
    static = drv.compose_toolbar_static()
    assert static is not None
    assert static.startswith(
        "imp · z9gixx · t · implementation phase · plan 1/1 · task 1/3")
    # (b) compose_status row 3 is that same position row
    block = drv.compose_status(0)
    assert block is not None and len(block) == 3
    assert block[2].startswith(
        "imp · z9gixx · t · implementation phase · plan 1/1 · task 1/3")


def test_driver_project_leads_the_startup_identity_row() -> None:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out),
                     startup_verb="resuming", project="imp",
                     quest_id="lufu50", title="Instant opening identity bar")
    rows = drv.compose_startup(0)
    assert rows is not None and len(rows) == 3
    assert rows[2] == "imp · lufu50 · Instant opening identity bar"


def test_driver_without_project_renders_todays_bands_unchanged() -> None:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out),
                     startup_verb="resuming", quest_id="lufu50", title="t")
    drv.on_event(ev("1", "quest_created", id="lufu50", title="t"))
    assert drv.compose_toolbar_static() == "lufu50 · t"
    rows = drv.compose_startup(0)
    assert rows is not None and rows[2] == "lufu50 · t"


# AC 5(a)
def test_context_bar_prefers_a_provider_reported_window() -> None:
    from imp.cli.live_driver import context_bar
    # codex reports 258400 for gpt-5.6-*; the catalog's 1.05M would draw 20%.
    # 206720/258400 = 0.80 = the handoff cutoff (soft_cap 260k > window, so the
    # cutoff is the 0.8 default) -> eight red cells.
    bar = context_bar("codex", "gpt-5.6-sol", 206_720, context_window=258_400,
                      color=True, over_bg=False)
    assert bar is not None
    assert bar.endswith("80%\x1b[0m")
    assert "\x1b[31m" in bar


# AC 5(b)
def test_context_bar_without_a_reported_window_falls_back_to_the_catalog() -> None:
    from imp.cli.live_driver import context_bar
    # Same call, no reading: unchanged behavior — 206720/1_050_000 = 20%.
    assert context_bar("codex", "gpt-5.6-sol", 206_720,
                       color=False, over_bg=False) == "██░░░░░░░░ 20%"


# AC 8
def test_compose_status_bar_uses_the_window_from_the_usage_patch() -> None:
    drv, _ = _driver()
    drv.on_event(ev("1", "quest_created", quest_id=1, title="t"))
    drv.on_event(ev("2", "step_started", step="s", kind="agent", phase="p",
                    model="gpt-5.6-sol", provider="codex"))
    drv.on_event(ev("3", "agent_message", agent_id="step:p/s", provider="codex",
                    model="gpt-5.6-sol", stop_reason="end_turn",
                    raw={"content": [{"type": "text", "text": "done"}]}, usage={}))
    drv.on_event({"type": "agent_message_usage_patch", "agent_id": "step:p/s",
                  "payload": {"usage": {"input_tokens": 82628},
                              "context_tokens": 22389, "context_window": 258400}})
    block = drv.compose_status(0)
    assert block is not None
    # 22389/258400 -> 9%. Against the catalog's 1.05M window it would read 2%.
    assert "9%" in block[2]
    assert "2%" not in block[2]


def test_compose_subagent_rows_take_the_window_from_a_usage_patch() -> None:
    from imp.cli.live_driver import compose_subagent_rows
    ctx = ev("c1", "step_context", agent_id="sub:t1", provider="codex",
             model="gpt-5.6-sol", tools_schema=[], params={}, max_tool_calls=None)
    msg = ev("r1", "agent_message", agent_id="sub:t1", provider="codex",
             model="gpt-5.6-sol", stop_reason="end_turn",
             raw={"content": [{"type": "text", "text": "hi"}]}, usage={})
    patch = {"type": "agent_message_usage_patch", "agent_id": "sub:t1",
             "payload": {"usage": {"input_tokens": 82628},
                         "context_tokens": 22389, "context_window": 258400}}
    b = _sub_builder(("t1", "coder"), readings=[ctx, msg, patch],
                     start_ts="2026-01-01T00:00:00+00:00")
    rows = compose_subagent_rows(b, color=False, width=120,
                                 now_iso="2026-01-01T00:00:05+00:00")
    assert len(rows) == 1
    assert "9%" in rows[0]      # the sub's own reading, against 258400 not 1.05M


def test_activity_countdown_renders_a_multi_day_wait_in_days() -> None:
    from imp.cli.live_driver import compose_activity_line
    # 187260s total, 60s elapsed -> 187200s left = 2d 4h.
    act = nv.Activity(verb="waiting for limit reset", countdown_total=187260.0,
                      start_ts="2026-01-01T00:00:00+00:00")
    line = compose_activity_line(activity=act, frame=0, color=False,
                                 now_iso="2026-01-01T00:01:00+00:00")

    assert "2d 4h left" in line

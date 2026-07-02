from __future__ import annotations

import io

from imp.cli.narrative import build_tree
from imp.cli.render_text import render_tree
from imp.cli.tui_driver import TuiDriver
from tests._event_fixtures import SAMPLE, ev


def _drv(**kw) -> TuiDriver:
    return TuiDriver(color=False, width=80, **kw)


def test_render_full_matches_render_tree_full() -> None:
    drv = _drv()
    for e in SAMPLE:
        drv.on_event(e)
    expected = "\n".join(render_tree(build_tree(SAMPLE), full=True,
                                     color=False, width=80, live_viewer=True))
    assert drv.render(full=True) == expected


def test_render_lean_matches_render_tree_lean() -> None:
    drv = _drv()
    for e in SAMPLE:
        drv.on_event(e)
    expected = "\n".join(render_tree(build_tree(SAMPLE), full=False,
                                     color=False, width=80, live_viewer=True))
    assert drv.render(full=False) == expected


def test_print_collapsed_transcript_emits_full_false_with_trailing_newline() -> None:
    drv = _drv()
    for e in SAMPLE:
        drv.on_event(e)
    buf = io.StringIO()
    drv.print_collapsed_transcript(buf)
    expected = "\n".join(render_tree(build_tree(SAMPLE), full=False,
                                     color=False, width=80, live_viewer=True)) + "\n"
    assert buf.getvalue() == expected


def test_on_event_invokes_invalidate_callback() -> None:
    calls: list[int] = []
    drv = _drv(on_invalidate=lambda: calls.append(1))
    drv.on_event(SAMPLE[0])
    drv.on_event(SAMPLE[1])
    assert calls == [1, 1]


def test_status_text_none_before_any_activity() -> None:
    assert _drv().status_text(0, 1.0) is None


def test_status_text_present_during_activity() -> None:
    drv = TuiDriver(color=True, width=80)
    drv.on_event(ev("1", "step_started", step="s", kind="agent", phase="p"))
    drv.on_event(ev("t1", "tool_call", agent_id="a", id="c1", name="bash",
                    input={"command": "ls"}))
    txt = drv.status_text(0, 1.0)
    assert txt is not None
    assert "bash ls" in txt



def test_seed_opening_intent_shows_in_render_and_invalidates() -> None:
    calls: list[int] = []
    drv = _drv(on_invalidate=lambda: calls.append(1))
    drv.seed_opening_intent("add a --json flag")
    assert "add a --json flag" in drv.render(full=False)
    assert calls == [1]


def test_seed_pending_reply_shows_in_render_and_invalidates() -> None:
    calls: list[int] = []
    drv = _drv(on_invalidate=lambda: calls.append(1))
    drv.seed_pending_reply("ship it")
    assert "ship it" in drv.render(full=False)
    assert calls == [1]


def test_render_full_suppresses_system_prompt_in_live_viewer() -> None:
    drv = _drv()
    drv.on_event(ev("2", "step_started", step="s", kind="agent", phase="p",
                    model="m", provider="anthropic"))
    drv.on_event(ev("3", "step_context", agent_id="step:p/s", provider="anthropic",
                    model="claude", system_prompt="SECRET SYSTEM PROMPT",
                    tools_schema=[{"name": "bash"}], params={}, max_tool_calls=50))
    out = drv.render(full=True)
    assert "SECRET SYSTEM PROMPT" not in out
    assert "- bash" not in out
    buf = io.StringIO()
    drv.print_collapsed_transcript(buf)
    transcript = buf.getvalue()
    assert "SECRET SYSTEM PROMPT" not in transcript
    # imp log --full (live_viewer default False) still renders it.
    log = "\n".join(render_tree(build_tree([
        ev("2", "step_started", step="s", kind="agent", phase="p",
           model="m", provider="anthropic"),
        ev("3", "step_context", agent_id="step:p/s", provider="anthropic",
           model="claude", system_prompt="SECRET SYSTEM PROMPT",
           tools_schema=[{"name": "bash"}], params={}, max_tool_calls=50),
    ]), full=True, color=False, width=80))
    assert "SECRET SYSTEM PROMPT" in log


def _terminal_pause_with_warning() -> list[dict]:
    # A reasoned terminal pause (max_loops) preceded by a runner_warning: the
    # step completes (pause nests as a phase child), then the run stops paused.
    return [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("3", "runner_warning", agent_id="step:design/spec", message="alpha"),
        ev("4", "step_completed", step="spec", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
        ev("5", "workflow_paused", reason="max_loops", interrupt_reason="max_loops"),
    ]


def test_finalize_flushes_terminal_pause_warnings_summary() -> None:
    events = _terminal_pause_with_warning()
    drv = _drv()
    for e in events:
        drv.on_event(e)

    # Before finalize the shared build_tree render already carries the summary
    # (build_tree finalizes internally), but the driver's own tree does not —
    # this is the dropped-summary regression the wiring fixes.
    before = io.StringIO()
    drv.print_collapsed_transcript(before)
    assert "warning this run" not in before.getvalue()

    drv.finalize()

    after = io.StringIO()
    drv.print_collapsed_transcript(after)
    assert "1 warning this run" in after.getvalue()
    # Live == imp log: the finalized transcript matches build_tree's render,
    # which finalizes its own builder over the same events.
    expected = "\n".join(render_tree(build_tree(events), full=False,
                                     color=False, width=80, live_viewer=True)) + "\n"
    assert after.getvalue() == expected


def test_finalize_no_summary_for_resumable_pause() -> None:
    # Pause then resume (run ends non-paused): finalize() is a no-op, so no
    # warnings summary is flushed even though a warning preceded the pause.
    events = [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("3", "runner_warning", agent_id="step:design/spec", message="alpha"),
        ev("4", "workflow_paused", reason="ctrl-c", interrupt_reason="ctrl-c"),
        ev("5", "workflow_resumed"),
    ]
    drv = _drv()
    for e in events:
        drv.on_event(e)
    drv.finalize()
    out = io.StringIO()
    drv.print_collapsed_transcript(out)
    assert "warning this run" not in out.getvalue()

# tests/test_tui_live.py
from __future__ import annotations

from imp.cli import narrative as nv
from imp.cli.tui import NarrativeApp


def _ev(eid, etype, agent_id=None, **payload):
    return {"id": eid, "agent_id": agent_id, "type": etype, "payload": payload}


def test_on_event_grows_tree_and_keeps_cursor_pinned_to_tail() -> None:
    app = NarrativeApp(nv.build_tree([]), live=True, mouse=False)
    assert app.follow_tail is True
    app.on_event(_ev("1", "change_created", change_id=1, title="t"))
    app.on_event(_ev("2", "phase_started", phase="design"))
    app.on_event(_ev("3", "step_started", step="spec", model="m", provider="anthropic"))
    # Sticky tail: cursor stays at the last line as content streams in.
    assert app.cursor_line == len(app._lines) - 1
    blob = "\n".join("".join(t for _s, t in ln.fragments) for ln in app._lines)
    assert "design" in blob and "spec" in blob


def test_scrolled_up_user_is_not_yanked_to_tail_on_new_event() -> None:
    app = NarrativeApp(nv.build_tree([]), live=True, mouse=False)
    for e in [_ev("1", "change_created", change_id=1, title="t"),
              _ev("2", "phase_started", phase="design"),
              _ev("3", "step_started", step="spec", model="m", provider="anthropic")]:
        app.on_event(e)
    app.goto_top()                          # user scrolls up
    assert app.follow_tail is False
    pinned = app.cursor_line
    app.on_event(_ev("4", "thinking", agent_id="step:design/spec", content="x"))
    assert app.cursor_line == pinned        # viewport didn't jump
    assert app.new_below_count >= 1         # footer "↓ N new" hint source


class _FakeCoord:
    def __init__(self):
        self.paused = 0
    async def signal_pause(self):
        self.paused += 1
    @property
    def should_pause(self):
        return self.paused > 0


def test_request_pause_is_idempotent_across_signal_and_keystroke() -> None:
    import asyncio
    async def go():
        coord = _FakeCoord()
        app = NarrativeApp(nv.build_tree([]), live=True, mouse=False, coordinator=coord)
        app.request_pause()          # keyboard Ctrl-C
        app.request_pause()          # again (or an OS signal racing in)
        # Two ticks: one to schedule the ensure_future task, one to run it.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert app.exit_reason == "pause"
        assert app._pausing is True  # guard set; double-pause prevented
        assert coord.paused == 1     # signal_pause delivered exactly once (not cancelled)
    asyncio.run(go())


def test_request_pause_resolves_a_pending_input_future() -> None:
    import asyncio
    from imp.cli.prompt import ESC_SENTINEL
    async def go():
        app = NarrativeApp(nv.build_tree([]), live=True, mouse=False, coordinator=_FakeCoord())
        fut = asyncio.ensure_future(app.request_line("> "))
        await asyncio.sleep(0)
        app.request_pause()          # pause mid-prompt must unwind the awaiter
        # the future resolves (sentinel) so run_change isn't left hanging
        assert await fut == ESC_SENTINEL
    asyncio.run(go())


def test_subagent_tokens_count_toward_footer_total() -> None:
    app = NarrativeApp(nv.build_tree([]), live=True, mouse=False)
    app.on_event(_ev("1", "change_created", change_id=1, title="t"))
    app.on_event(_ev("tk", "tool_call", agent_id=None, id="b1", name="task",
                     input={"subagent_name": "rev", "task_description": "go"}))
    # NB: a task tool_call at root has no open step; builder attaches at root.
    app.on_event(_ev("s", "llm_response", agent_id="sub:tk", provider="anthropic",
                     model="m", stop_reason="end_turn",
                     usage={"input_tokens": 99, "output_tokens": 9}, raw={"content": []}))
    assert app.tree.tokens.input == 99      # subagent tokens included (F2)

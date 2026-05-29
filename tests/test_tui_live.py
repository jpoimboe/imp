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


def test_run_live_tui_interrupt_does_not_raise() -> None:
    """Ctrl-C path: request_pause() calls application.exit() (1st call), then
    _engine's finally calls application.exit() again (2nd call).  The guard in
    tui.py must swallow the second call so run_live_tui returns without raising
    an ExceptionGroup.  The deterministic mechanism: the fake engine awaits a
    Future that is never resolved; we call app_obj.request_pause() directly via
    a callback injected through on_event, so no real keyboard/OS event is needed.
    run_live_tui must return (not raise) and exit_reason must be 'pause'."""
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput


    class _Coord:
        should_pause = False
        async def signal_pause(self): ...
        def attach_runner(self, fn): ...
        def detach_runner(self): ...

    # Sentinel event type used to trigger the pause from within on_event.
    async def fake_run_change(*, on_event, **kw):
        # Emit the sentinel event; on_event will call request_pause().
        on_event({"id": "x", "agent_id": None, "type": "_test_pause", "payload": {}})
        # Now park here forever — the pause should unwind us via CancelledError.
        import asyncio
        await asyncio.Future()   # never resolves; cancelled by tg.cancel_scope.cancel()

    # We need a handle to app_obj so we can wire on_event → request_pause.
    # Monkey-patch NarrativeApp to capture the instance, then inject the hook.
    from imp.cli import tui as tui_mod

    original_init = tui_mod.NarrativeApp.__init__
    captured = {}

    def patched_init(self, *a, **kw):
        original_init(self, *a, **kw)
        captured["app"] = self

    tui_mod.NarrativeApp.__init__ = patched_init
    try:

        # We wrap on_event after build so we can trigger pause on sentinel.
        original_run_live_tui = tui_mod.run_live_tui

        def patched_run_live_tui(**kwargs):
            orig_run_change = kwargs["run_change"]

            async def hooked_run_change(*, on_event, **kw2):
                app = captured.get("app")

                def hooked_on_event(event):
                    on_event(event)
                    if event.get("type") == "_test_pause" and app is not None:
                        app.request_pause()

                return await orig_run_change(on_event=hooked_on_event, **kw2)

            kwargs["run_change"] = hooked_run_change
            return original_run_live_tui(**kwargs)

        with create_pipe_input() as inp:
            # Must not raise — this is the key assertion.
            result = patched_run_live_tui(
                run_change=fake_run_change, coordinator=_Coord(), mouse=False,
                _input=inp, _output=DummyOutput())
    finally:
        tui_mod.NarrativeApp.__init__ = original_init

    # Engine never completed, so result_box is empty → None.
    assert result is None
    # exit_reason was set to "pause" by request_pause().
    assert captured["app"].exit_reason == "pause"


def test_run_live_tui_drives_fake_engine_and_returns_completed(monkeypatch) -> None:
    """The live wiring: app + fake engine in one task group; on_event feeds the
    builder; engine completion exits the app. Uses a DummyOutput + pipe input."""
    import anyio
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from imp.cli.tui import run_live_tui
    from imp.engine.executor import RunResult

    events = [
        {"id": "1", "agent_id": None, "type": "change_created",
         "payload": {"change_id": 1, "title": "t"}},
        {"id": "2", "agent_id": None, "type": "phase_started", "payload": {"phase": "p"}},
    ]

    class _Coord:
        should_pause = False
        async def signal_pause(self): ...
        def attach_runner(self, fn): ...
        def detach_runner(self): ...

    async def fake_run_change(*, on_event, **kw):
        for e in events:
            on_event(e)
            await anyio.sleep(0)
        return RunResult.COMPLETED

    with create_pipe_input() as inp:
        result = run_live_tui(
            run_change=fake_run_change, coordinator=_Coord(), mouse=False,
            _input=inp, _output=DummyOutput())
    assert result is RunResult.COMPLETED


def test_resume_seed_plus_reemission_dedups_and_completes() -> None:
    import anyio
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from imp.cli.tui import run_live_tui
    from imp.engine.executor import RunResult

    # Prior log of an interrupted run, ending with the resume marker (exactly
    # what _async_cmd_resume snapshots after appending workflow_resumed).
    seed = [
        _ev("c", "change_created", change_id=1, title="t"),
        _ev("p", "phase_started", phase="design"),
        _ev("s1", "step_started", step="spec", model="m", provider="anthropic"),
        _ev("s1c", "step_completed", step="spec", action="continue"),
        _ev("s2", "step_started", step="build", model="m", provider="anthropic"),
        _ev("ll", "llm_response", agent_id="step:design/build", provider="anthropic",
            model="m", stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "partial"}]}),
        _ev("wp", "workflow_paused", reason="ctrl-c"),
        _ev("wr", "workflow_resumed"),
    ]
    # What the executor re-emits on resume (re-runs the current step from scratch).
    reemit = [
        _ev("p2", "phase_started", phase="design"),
        _ev("s2b", "step_started", step="build", model="m", provider="anthropic"),
        _ev("ll2", "llm_response", agent_id="step:design/build", provider="anthropic",
            model="m", stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "fresh"}]}),
        _ev("s2c", "step_completed", step="build", action="continue"),
        _ev("pc", "phase_completed", phase="design"),
    ]

    # (1) Model dedup (guaranteed by Task 10B): one design phase, one build step.
    tree = nv.build_tree(seed + reemit)
    phases = [c for c in tree.root.children if isinstance(c, nv.PhaseNode)]
    assert len(phases) == 1
    builds = [s for s in phases[0].children
              if isinstance(s, nv.StepNode) and s.name == "build"]
    assert len(builds) == 1

    # (2) Wiring: seed installed before the engine emits; runs to COMPLETED.
    class _Coord:
        should_pause = False
        async def signal_pause(self): ...
        async def run_signal_receiver(self): ...
        def attach_runner(self, fn): ...
        def detach_runner(self): ...

    async def fake_run_change(*, on_event, **kw):
        for e in reemit:
            on_event(e)
            await anyio.sleep(0)
        return RunResult.COMPLETED

    with create_pipe_input() as inp:
        result = run_live_tui(run_change=fake_run_change, coordinator=_Coord(),
                              mouse=False, seed_events=seed,
                              _input=inp, _output=DummyOutput())
    assert result is RunResult.COMPLETED

from __future__ import annotations

import argparse
import sys as _sys
from types import SimpleNamespace

import anyio
import pytest

import imp.cli.main as m
from imp.engine.executor import RunResult


def test_build_parser_new_accepts_tui_flag() -> None:
    ns = m._build_parser().parse_args(["new", "--tui", "a title"])
    assert ns.tui is True


def test_build_parser_new_tui_defaults_false() -> None:
    ns = m._build_parser().parse_args(["new"])
    assert ns.tui is False


def test_build_parser_resume_accepts_tui_flag() -> None:
    ns = m._build_parser().parse_args(["resume", "2026-06-11-x", "--tui"])
    assert ns.tui is True


def test_build_parser_resume_tui_defaults_false() -> None:
    ns = m._build_parser().parse_args(["resume", "2026-06-11-x"])
    assert ns.tui is False


class _NotTty:
    def isatty(self) -> bool:
        return False


class _Tty:
    def isatty(self) -> bool:
        return True


def test_warn_if_tui_unavailable_warns_on_non_tty(capsys) -> None:
    m._warn_if_tui_unavailable(argparse.Namespace(tui=True), _NotTty())
    assert "interactive terminal" in capsys.readouterr().err


def test_warn_if_tui_unavailable_silent_on_tty(capsys) -> None:
    m._warn_if_tui_unavailable(argparse.Namespace(tui=True), _Tty())
    assert capsys.readouterr().err == ""


def test_warn_if_tui_unavailable_silent_without_flag(capsys) -> None:
    m._warn_if_tui_unavailable(argparse.Namespace(tui=False), _NotTty())
    assert capsys.readouterr().err == ""


def test_is_benign_interrupt_teardown_matches_killed_child_writes() -> None:
    import asyncio

    class CLIConnectionError(Exception):
        pass

    # The two detached-task exceptions a process-group interrupt leaves behind.
    assert m._is_benign_interrupt_teardown(
        CLIConnectionError("Cannot write to terminated process (exit code: 0)"))
    assert m._is_benign_interrupt_teardown(asyncio.InvalidStateError("invalid state"))
    # A real provider failure (or no exception) must still surface.
    assert not m._is_benign_interrupt_teardown(
        CLIConnectionError("connection refused"))
    assert not m._is_benign_interrupt_teardown(RuntimeError("boom"))
    assert not m._is_benign_interrupt_teardown(None)


def test_is_benign_interrupt_teardown_matches_exit_143_process_error() -> None:
    # A stand-in matched by class NAME (like the CLIConnectionError test above),
    # mirroring the real claude_agent_sdk ProcessError(message, exit_code=None).
    class ProcessError(Exception):
        def __init__(self, message: str, exit_code: int | None = None) -> None:
            super().__init__(
                f"{message} (exit code: {exit_code})" if exit_code is not None
                else message)
            self.exit_code = exit_code

    assert m._is_benign_interrupt_teardown(
        ProcessError("Command failed with exit code 143", exit_code=143))
    assert m._is_benign_interrupt_teardown(
        ProcessError("commande interrompue", exit_code=143))
    assert m._is_benign_interrupt_teardown(
        ProcessError("Command failed with exit code 143 (exit code: 143)"))

    class NoAttrError(Exception):
        pass

    assert m._is_benign_interrupt_teardown(
        NoAttrError("Command failed with exit code 143 (exit code: 143)"))
    assert not m._is_benign_interrupt_teardown(
        ProcessError("Command failed with exit code 1", exit_code=1))


def test_new_tui_on_non_tty_warns_and_streams(tmp_path, monkeypatch, capsys) -> None:
    from imp.engine.executor import RunResult
    from tests.conftest import _seed_control

    _seed_control(tmp_path)

    async def fake_run_change(change, **kwargs):
        on_event = kwargs["on_event"]
        on_event({"id": "1", "agent_id": None, "type": "change_created",
                  "payload": {"slug": change.slug, "title": change.title}})
        on_event({"id": "2", "agent_id": None, "type": "step_started",
                  "payload": {"step": "s", "kind": "agent", "phase": "design",
                              "model": "x", "provider": "anthropic"}})
        return RunResult.COMPLETED

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "run_change", fake_run_change)

    # Under pytest's capture, sys.stdout.isatty() is False → the non-tty path.
    args = argparse.Namespace(title="demo", tui=True)
    rc = anyio.run(m._async_cmd_new, args)
    out, err = capsys.readouterr()
    assert rc == 0
    assert "interactive terminal" in err     # warning surfaced on stderr
    assert "design" in out                    # still streamed via LiveDriver


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _FakeTtyStdout:
    def isatty(self) -> bool:
        return True


def test_tui_requested_true_only_on_tty(monkeypatch) -> None:
    monkeypatch.setattr(_sys, "stdout", _FakeTtyStdout())
    assert m._tui_requested(argparse.Namespace(tui=True)) is True
    assert m._tui_requested(argparse.Namespace(tui=False)) is False


def test_tui_requested_false_on_non_tty() -> None:
    # Under pytest capture sys.stdout.isatty() is False.
    assert m._tui_requested(argparse.Namespace(tui=True)) is False


class _FakeTuiApp:
    def __init__(self, **kwargs: object) -> None:
        self.coord = kwargs.get("coord")
        self.events: list[object] = []
        self.transcript_written = False
        self._exit = False

    def on_event(self, event) -> None:
        self.events.append(event)

    async def solicit(self, change) -> str:
        return ""

    async def run_async(self) -> None:
        import anyio as _anyio
        while not self._exit:
            await _anyio.sleep(0.005)

    def exit(self) -> None:
        self._exit = True

    def finish(self) -> None:
        pass

    def print_collapsed_transcript(self, stream) -> None:
        self.transcript_written = True
        stream.write("COLLAPSED-TRANSCRIPT\n")


@pytest.mark.anyio
async def test_run_tui_session_streams_no_esc_reader_then_handles_outcome(
    monkeypatch, capsys,
) -> None:
    captured: list[_FakeTuiApp] = []
    outcomes: list[RunResult] = []

    class _CapturingApp(_FakeTuiApp):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)
            captured.append(self)

    async def _fake_run_change(change, *, on_event, user_chat_input_fn,
                               coordinator, non_interactive, seed_opening=None):
        on_event({"id": "1", "agent_id": None, "type": "change_created",
                  "payload": {"slug": "s", "title": "t"}})
        return RunResult.COMPLETED

    def _boom_esc_reader(*a, **k):
        raise AssertionError("EscKeyReader must not be started in --tui mode")

    monkeypatch.setattr(m, "TuiApp", _CapturingApp)
    monkeypatch.setattr(m, "run_change", _fake_run_change)
    monkeypatch.setattr(m, "EscKeyReader", _boom_esc_reader)
    monkeypatch.setattr(m, "_maybe_build_channel", lambda change: None)
    monkeypatch.setattr(m, "_handle_terminal_outcome",
                        lambda change, result, coord: outcomes.append(result) or 0)

    rc = await m._run_tui_session(SimpleNamespace(), args=argparse.Namespace(tui=True))

    assert rc == 0
    assert outcomes == [RunResult.COMPLETED]
    assert captured and captured[0].events
    assert captured[0].transcript_written is True
    assert "COLLAPSED-TRANSCRIPT" in capsys.readouterr().out


@pytest.mark.anyio
async def test_run_tui_session_result_none_calls_finalize_paused(
    monkeypatch, capsys,
) -> None:
    finalized: list[str] = []

    class _QuickExitApp(_FakeTuiApp):
        async def run_async(self) -> None:
            pass  # exits immediately, before run_change completes

    async def _slow_run_change(change, **kwargs):
        import anyio as _anyio
        await _anyio.sleep(100)  # never completes

    monkeypatch.setattr(m, "TuiApp", _QuickExitApp)
    monkeypatch.setattr(m, "run_change", _slow_run_change)
    monkeypatch.setattr(m, "_maybe_build_channel", lambda change: None)
    monkeypatch.setattr(m, "_finalize_paused_exit",
                        lambda change, driver, coord, reason: finalized.append(reason) or 130)

    rc = await m._run_tui_session(SimpleNamespace(), args=argparse.Namespace(tui=True))

    assert rc == 130
    assert finalized


@pytest.mark.anyio
async def test_run_tui_session_load_error_returns_2(monkeypatch, capsys) -> None:
    from imp.engine.errors import LoadError

    async def _error_run_change(change, **kwargs):
        raise LoadError("bad config")

    monkeypatch.setattr(m, "TuiApp", _FakeTuiApp)
    monkeypatch.setattr(m, "run_change", _error_run_change)
    monkeypatch.setattr(m, "_maybe_build_channel", lambda change: None)

    rc = await m._run_tui_session(SimpleNamespace(), args=argparse.Namespace(tui=True))

    assert rc == 2
    assert "bad config" in capsys.readouterr().err


@pytest.mark.anyio
async def test_run_tui_session_composes_channel_on_event(monkeypatch, capsys) -> None:
    channel_events: list[object] = []

    class _FakeChannel:
        async def start(self, tg: object) -> bool:
            return True

        def on_event(self, event: object) -> None:
            channel_events.append(event)

    async def _fake_run_change(change, *, on_event, **kwargs):
        on_event({"id": "1", "agent_id": None, "type": "change_created",
                  "payload": {"slug": "s", "title": "t"}})
        return RunResult.COMPLETED

    monkeypatch.setattr(m, "TuiApp", _FakeTuiApp)
    monkeypatch.setattr(m, "run_change", _fake_run_change)
    monkeypatch.setattr(m, "_maybe_build_channel", lambda change: _FakeChannel())
    monkeypatch.setattr(m, "_handle_terminal_outcome",
                        lambda change, result, coord: 0)

    rc = await m._run_tui_session(SimpleNamespace(), args=argparse.Namespace(tui=True))

    assert rc == 0
    assert any(e.get("type") == "change_created" for e in channel_events)


@pytest.mark.anyio
async def test_benign_loop_handler_swallows_paused_and_benign_then_restores() -> None:
    import asyncio
    from types import SimpleNamespace

    class ProcessError(Exception):
        def __init__(self, message: str, exit_code: int | None = None) -> None:
            super().__init__(message)
            self.exit_code = exit_code

    loop = asyncio.get_running_loop()
    seen: list[dict] = []

    def prior(lp, ctx):
        seen.append(ctx)

    loop.set_exception_handler(prior)
    coord = SimpleNamespace(should_pause=False)

    with m._benign_interrupt_loop_handler(coord):
        # benign exit-143 teardown → swallowed (prior NOT invoked)
        loop.call_exception_handler(
            {"message": "x", "exception": ProcessError("boom", exit_code=143)})
        assert seen == []
        # a real error → routed to the prior handler
        loop.call_exception_handler(
            {"message": "y", "exception": RuntimeError("real")})
        assert len(seen) == 1
        # while a pause is in flight EVERYTHING is swallowed
        coord.should_pause = True
        loop.call_exception_handler(
            {"message": "z", "exception": RuntimeError("during-pause")})
        assert len(seen) == 1

    # the prior handler is restored on context exit
    assert loop.get_exception_handler() is prior


def test_async_cmd_new_installs_benign_loop_handler(tmp_path, monkeypatch) -> None:
    import contextlib

    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    entered = {"n": 0}

    @contextlib.contextmanager
    def fake_handler(coord):
        entered["n"] += 1
        yield

    async def fake_session(change, *, tg, **kwargs):
        tg.cancel_scope.cancel()
        return 0

    monkeypatch.setattr(m, "_benign_interrupt_loop_handler", fake_handler)
    monkeypatch.setattr(m, "_run_change_session", fake_session)

    rc = anyio.run(m._async_cmd_new, argparse.Namespace(
        title="demo", tui=False, non_interactive=True, message="go"))
    assert rc == 0
    assert entered["n"] == 1

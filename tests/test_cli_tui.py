from __future__ import annotations

import argparse
import sys as _sys
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

import imp.cli.main as m
from imp.engine.change import Change
from imp.engine.executor import RunResult
from imp.engine.state import State


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


def test_new_tui_on_non_tty_warns_and_routes_to_session_client(
        tmp_path, monkeypatch, capsys) -> None:
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    called = {"n": 0}

    async def _fake_client(change, *, driver, coord, solicit,
                           seed_opening=None, status=None,
                           leave_mode=None, leave_state=None, title=None,
                           autonomous=False) -> int:
        called["n"] += 1
        return 0

    monkeypatch.setattr(m, "run_session_client", _fake_client)

    # Under pytest's capture, sys.stdout.isatty() is False → --tui falls back to
    # the streaming session-client path.
    args = argparse.Namespace(title="demo", tui=True)
    rc = anyio.run(m._async_cmd_new, args)
    _out, err = capsys.readouterr()
    assert rc == 0
    assert "interactive terminal" in err     # warning surfaced on stderr
    assert called["n"] == 1                   # routed to the session client


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


@pytest.mark.anyio
async def test_run_tui_session_renders_events_and_handles_outcome(
    monkeypatch, capsys,
) -> None:
    captured: list[_FakeTuiApp] = []
    outcomes: list[RunResult] = []

    class _CapturingApp(_FakeTuiApp):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)
            captured.append(self)

    async def _fake_terminal_client(
        *, attach, driver, coord, solicit, change_id: str = "",
        control_root=None, leave_state=None, leave_mode=None,
    ) -> None:
        driver.on_event({"id": "1", "agent_id": None, "type": "change_created",
                         "payload": {"slug": "s", "title": "t"}})

    monkeypatch.setattr(m, "TuiApp", _CapturingApp)
    monkeypatch.setattr(m, "run_terminal_client", _fake_terminal_client)
    monkeypatch.setattr(m, "build_session_attach",
                        lambda *a, **k: (lambda on_event: None))
    monkeypatch.setattr(m, "infer_session_result", lambda change: RunResult.COMPLETED)
    monkeypatch.setattr(m, "_handle_terminal_outcome",
                        lambda change, result, coord: outcomes.append(result) or 0)

    rc = await m._run_tui_session(SimpleNamespace(id="fake-id", control_root=None))

    assert rc == 0
    assert outcomes == [RunResult.COMPLETED]
    assert captured and captured[0].events            # event reached the TUI driver
    assert captured[0].transcript_written is True
    assert "COLLAPSED-TRANSCRIPT" in capsys.readouterr().out


@pytest.mark.anyio
async def test_run_tui_session_paused_returns_130(monkeypatch, capsys) -> None:
    async def _fake_terminal_client(
        *, attach, driver, coord, solicit, change_id: str = "",
        control_root=None, leave_state=None, leave_mode=None,
    ) -> None:
        await coord.signal_pause(reason="ctrl-c")     # sets should_pause + reason

    monkeypatch.setattr(m, "TuiApp", _FakeTuiApp)
    monkeypatch.setattr(m, "run_terminal_client", _fake_terminal_client)
    monkeypatch.setattr(m, "build_session_attach",
                        lambda *a, **k: (lambda on_event: None))
    monkeypatch.setattr(m, "infer_session_result", lambda change: RunResult.PAUSED)
    monkeypatch.setattr(m, "_handle_terminal_outcome", lambda c, r, co: 1)

    rc = await m._run_tui_session(SimpleNamespace(id="fake-id", control_root=None))

    assert rc == 130


@pytest.mark.anyio
async def test_run_tui_session_solicit_routes_to_app_solicit(monkeypatch) -> None:
    replies: list[str] = []

    class _SolicitApp(_FakeTuiApp):
        async def solicit(self, change) -> str:
            return "the reply"

    async def _fake_terminal_client(
        *, attach, driver, coord, solicit, change_id: str = "",
        control_root=None, leave_state=None, leave_mode=None,
    ) -> None:
        replies.append(await solicit("prompt?"))

    monkeypatch.setattr(m, "TuiApp", _SolicitApp)
    monkeypatch.setattr(m, "run_terminal_client", _fake_terminal_client)
    monkeypatch.setattr(m, "build_session_attach",
                        lambda *a, **k: (lambda on_event: None))
    monkeypatch.setattr(m, "infer_session_result", lambda change: RunResult.COMPLETED)
    monkeypatch.setattr(m, "_handle_terminal_outcome", lambda c, r, co: 0)

    rc = await m._run_tui_session(SimpleNamespace(id="fake-id", control_root=None))

    assert rc == 0
    assert replies == ["the reply"]



def test_resume_tui_routes_to_run_tui_session(tmp_path, monkeypatch) -> None:
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    ch = Change.create(Path(tmp_path), title="demo", change_id="feat",
                       base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "paused"
    st.save(ch.state_dir)

    seen: dict[str, object] = {}

    async def _fake_tui(change, *, seed_opening=None, resume=False,
                        leave_state=None, autonomous=False) -> int:
        seen["resume"] = resume
        return 0

    monkeypatch.setattr(m, "_run_tui_session", _fake_tui)
    monkeypatch.setattr(m, "_tui_requested", lambda _a: True)

    args = argparse.Namespace(token="feat", tui=True,
                              non_interactive=False, message=None)
    rc = anyio.run(m._async_cmd_resume, args)

    assert rc == 0
    assert seen == {"resume": True}


@pytest.mark.anyio
async def test_run_tui_session_threads_provided_leave_state(monkeypatch) -> None:
    async def _fake_terminal_client(
        *, attach, driver, coord, solicit, change_id: str = "",
        control_root=None, leave_state=None, leave_mode=None,
    ) -> None:
        leave_state["intent"] = "clear"

    monkeypatch.setattr(m, "TuiApp", _FakeTuiApp)
    monkeypatch.setattr(m, "run_terminal_client", _fake_terminal_client)
    monkeypatch.setattr(m, "build_session_attach",
                        lambda *a, **k: (lambda on_event: None))
    monkeypatch.setattr(m, "infer_session_result", lambda change: RunResult.COMPLETED)
    monkeypatch.setattr(m, "_handle_terminal_outcome", lambda c, r, co: 0)

    ls: dict[str, str] = {}
    rc = await m._run_tui_session(
        SimpleNamespace(id="fake-id", control_root=None), leave_state=ls)

    assert rc == 0
    assert ls["intent"] == "clear"

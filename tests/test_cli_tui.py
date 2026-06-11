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


_created: list[object] = []


class _FakeTuiApp:
    def __init__(self, *, coord, color, width) -> None:
        self.coord = coord
        self.events: list[object] = []
        self.transcript_written = False
        self._exit = False
        _created.append(self)

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
    _created.clear()
    outcomes: list[RunResult] = []

    async def _fake_run_change(change, *, on_event, user_chat_input_fn,
                               coordinator, non_interactive):
        on_event({"id": "1", "agent_id": None, "type": "change_created",
                  "payload": {"slug": "s", "title": "t"}})
        return RunResult.COMPLETED

    def _boom_esc_reader(*a, **k):
        raise AssertionError("EscKeyReader must not be started in --tui mode")

    monkeypatch.setattr(m, "TuiApp", _FakeTuiApp)
    monkeypatch.setattr(m, "run_change", _fake_run_change)
    monkeypatch.setattr(m, "EscKeyReader", _boom_esc_reader)
    monkeypatch.setattr(m, "_handle_terminal_outcome",
                        lambda change, result, coord: outcomes.append(result) or 0)

    rc = await m._run_tui_session(SimpleNamespace(), args=argparse.Namespace(tui=True))

    assert rc == 0
    assert outcomes == [RunResult.COMPLETED]
    assert _created and _created[0].events            # events streamed via on_event
    assert _created[0].transcript_written is True     # collapsed transcript printed
    assert "COLLAPSED-TRANSCRIPT" in capsys.readouterr().out

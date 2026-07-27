from __future__ import annotations

import argparse
import os
from types import SimpleNamespace

import anyio
import pytest

import imp.cli.main as m
import imp.cli.signals as sig
from imp.cli.main import _resume_live_framing
from imp.engine.quest import Quest
from imp.engine.state import State
from tests.conftest import _seed_control


def test_not_live_lock_is_plain_resuming_with_no_notice() -> None:
    marker, notice = _resume_live_framing(
        "feat", lock_live=False, halt_condition=None)
    assert marker == "resuming"
    assert notice is None


def test_not_live_lock_ignores_halt_condition() -> None:
    marker, notice = _resume_live_framing(
        "feat", lock_live=False, halt_condition="waiting")
    assert marker == "resuming"
    assert notice is None


def test_live_lock_non_waiting_reads_already_running() -> None:
    marker, notice = _resume_live_framing(
        "feat", lock_live=True, halt_condition=None)
    assert marker == "already running"
    assert notice == (
        "quest feat is already running in a live session — attaching to it.")


def test_live_lock_error_halt_still_reads_already_running() -> None:
    marker, notice = _resume_live_framing(
        "feat", lock_live=True, halt_condition="error")
    assert marker == "already running"
    assert notice == (
        "quest feat is already running in a live session — attaching to it.")


def test_live_lock_waiting_reads_already_waiting() -> None:
    marker, notice = _resume_live_framing(
        "feat", lock_live=True, halt_condition="waiting")
    assert marker == "already waiting"
    assert notice == (
        "quest feat is already waiting for input — attaching to answer it.")


def test_notice_interpolates_the_quest_id() -> None:
    _marker, notice = _resume_live_framing(
        "zzz123", lock_live=True, halt_condition="waiting")
    assert notice == (
        "quest zzz123 is already waiting for input — attaching to answer it.")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _seed_live(tmp_path, *, halt_condition=None, live=False) -> Quest:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="demo", quest_id="feat", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "paused"
    st.halt_condition = halt_condition
    st.save(ch.state_dir)
    if live:
        (ch.quest_dir / "lock").write_text(str(os.getpid()))
    return ch


def _patch_headless(monkeypatch) -> None:
    monkeypatch.setattr(m, "should_use_color", lambda *a, **k: False)
    monkeypatch.setattr(m, "_select_headless", lambda args: (True, False))
    monkeypatch.setattr(m, "make_headless_solicit", lambda *a, **k: None)
    monkeypatch.setattr(m, "LiveStatus", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(m, "_build_resume_driver", lambda **k: SimpleNamespace())
    monkeypatch.setattr(sig, "InterruptCoordinator", lambda *a, **k: SimpleNamespace())

    async def _ok(*a: object, **k: object) -> int:
        return 0

    monkeypatch.setattr(m, "run_session_client", _ok)


def test_headless_live_resume_marks_already_running_and_notice(
    tmp_path, monkeypatch, capsys,
) -> None:
    _seed_live(tmp_path, live=True)
    monkeypatch.chdir(tmp_path)
    _patch_headless(monkeypatch)

    rc = anyio.run(m._async_cmd_resume, argparse.Namespace(
        token="feat", non_interactive=True, message="go", autonomous=False))
    out = capsys.readouterr().out

    assert rc == 0
    assert "feat · demo · already running" in out
    assert (
        "quest feat is already running in a live session — attaching to it."
    ) in out


def test_headless_stopped_resume_stays_plain_resuming_no_notice(
    tmp_path, monkeypatch, capsys,
) -> None:
    _seed_live(tmp_path, live=False)   # no live lock -> plain resuming
    monkeypatch.chdir(tmp_path)
    _patch_headless(monkeypatch)

    rc = anyio.run(m._async_cmd_resume, argparse.Namespace(
        token="feat", non_interactive=True, message="go", autonomous=False))
    out = capsys.readouterr().out

    assert rc == 0
    assert "feat · demo · resuming" in out
    assert "already" not in out
    assert "attaching" not in out


def test_live_waiting_threads_already_waiting_into_inert_path(
    tmp_path, monkeypatch,
) -> None:
    _seed_live(tmp_path, halt_condition="waiting", live=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "should_use_color", lambda *a, **k: False)
    monkeypatch.setattr(m, "_select_headless", lambda args: (False, False))
    monkeypatch.setattr(m, "bare_turn_end_question", lambda quest: "Pending Q?")
    seen: dict[str, object] = {}

    async def fake_inert(quest, question, *, marker, notice, autonomous=False):
        seen["marker"] = marker
        seen["notice"] = notice
        return 0

    monkeypatch.setattr(m, "_resume_inert", fake_inert)

    rc = anyio.run(m._async_cmd_resume, argparse.Namespace(
        token="feat", non_interactive=False, message=None, autonomous=False))

    assert rc == 0
    assert seen["marker"] == "already waiting"
    assert seen["notice"] == (
        "quest feat is already waiting for input — attaching to answer it.")


def test_interactive_live_resume_marks_already_running_and_notice(
    tmp_path, monkeypatch, capsys,
) -> None:
    _seed_live(tmp_path, live=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "should_use_color", lambda *a, **k: False)
    monkeypatch.setattr(m, "_select_headless", lambda args: (False, False))
    monkeypatch.setattr(m, "_print_splash_banner", lambda *a, **k: False)

    async def _ok_loop(control_root, base, *, first):
        return 0

    monkeypatch.setattr(m, "_clear_loop", _ok_loop)

    rc = anyio.run(m._async_cmd_resume, argparse.Namespace(
        token="feat", non_interactive=False, message="go", autonomous=False))
    out = capsys.readouterr().out

    assert rc == 0
    assert "feat · demo · already running" in out
    assert (
        "quest feat is already running in a live session — attaching to it."
    ) in out

# tests/test_resume_startup_wiring.py
from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

import imp.cli.main as m
import imp.cli.signals as sig
from imp.engine.quest import Quest
from imp.engine.state import State
from tests._event_fixtures import agent_text, ev, task_lead_started
from tests.conftest import _seed_control


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _seed(tmp_path: Path, *, step_label: str | None = None) -> Quest:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="demo", quest_id="feat", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "paused"
    st.step_label = step_label
    st.save(ch.state_dir)
    return ch


def _capture_driver(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    class _CapDriver:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def install_startup_status(self) -> None: ...

        def echo_submitted_reply(self, text: str) -> None: ...

    monkeypatch.setattr("imp.cli.live_driver.LiveDriver", _CapDriver)
    monkeypatch.setattr("imp.cli.live_status.LiveStatus", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(sig, "InterruptCoordinator", lambda *a, **k: SimpleNamespace())
    return captured


def test_interactive_resume_derives_verb_from_step_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(tmp_path, step_label="reviewing spec")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_print_splash_banner", lambda **k: True)
    monkeypatch.setattr(m, "_is_interactive", lambda **k: True)
    monkeypatch.setattr(m, "should_use_color", lambda *a, **k: False)
    monkeypatch.setattr("imp.cli.session_run.make_interactive_solicit", lambda *a, **k: None)
    captured = _capture_driver(monkeypatch)

    async def _ok(*a: object, **k: object) -> int:
        return 0

    monkeypatch.setattr("imp.cli.session_run.run_session_client", _ok)

    async def _fake_loop(control, base, *, first, leave_mode):
        return await first({})

    monkeypatch.setattr(m, "_clear_loop", _fake_loop)

    monkeypatch.setattr(m.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(m.sys.stdout, "isatty", lambda: True)
    rc = anyio.run(m._async_cmd_resume, argparse.Namespace(
        token="feat", non_interactive=False, message=None))

    assert rc == 0
    assert captured["startup_verb"] == "reviewing spec"     # labeled step → its label
    assert captured["display_width"] is m._display_width     # real terminal width


def test_headless_resume_unlabeled_step_uses_random_phrase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(tmp_path)                                         # unlabeled step → step_label None
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "should_use_color", lambda *a, **k: False)
    monkeypatch.setattr(m, "_select_headless", lambda args: (True, False))
    monkeypatch.setattr("imp.cli.session_run.make_headless_solicit", lambda *a, **k: None)
    monkeypatch.setattr("imp.agents.random_status.random_status_phrase", lambda: "scheming")
    captured = _capture_driver(monkeypatch)

    async def _ok(*a: object, **k: object) -> int:
        return 0

    monkeypatch.setattr("imp.cli.session_run.run_session_client", _ok)

    rc = anyio.run(m._async_cmd_resume, argparse.Namespace(
        token="feat", non_interactive=True, message="go",
        autonomous=False))

    assert rc == 0
    assert captured["startup_verb"] == "scheming"           # unlabeled → random imp phrase
    assert captured["display_width"] is m._display_width
    assert captured.get("preceded_by_blank", False) is False


@pytest.mark.anyio
async def test_inert_resume_derives_verb_and_passes_display_width(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _seed(tmp_path, step_label="writing plan")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "should_use_color", lambda *a, **k: False)
    monkeypatch.setattr("imp.cli.session_run.make_interactive_solicit", lambda *a, **k: None)
    monkeypatch.setattr("imp.cli.inert_resume.make_buffered_solicit", lambda line, inner: None)
    captured = _capture_driver(monkeypatch)

    async def _ok(*a: object, **k: object) -> int:
        return 0

    monkeypatch.setattr("imp.cli.session_run.run_session_client", _ok)

    async def _fake_run_inert(*, question, ask_line, launch):
        return await launch("my answer")

    monkeypatch.setattr("imp.cli.inert_resume.run_inert_resume", _fake_run_inert)

    async def _fake_loop(control, base, *, first, leave_mode):
        return await first({})

    monkeypatch.setattr(m, "_clear_loop", _fake_loop)

    rc = await m._resume_inert(ch, "Pending Q?", marker="resuming", notice=None)

    assert rc == 0
    assert captured["startup_verb"] == "writing plan"       # labeled step → its label
    assert captured["display_width"] is m._display_width     # item 7: inert now passes width
    assert captured["preceded_by_blank"] is False


def test_build_resume_driver_carries_the_project_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _seed(tmp_path, step_label="reviewing spec")
    st = State.load(ch.state_dir)
    captured = _capture_driver(monkeypatch)
    m._build_resume_driver(quest=ch, state=st, status=SimpleNamespace(), color=False,
                           stream=SimpleNamespace())
    assert captured["project"] == ch.control_root.name == tmp_path.name


# Resume tail AC5(a): headless builds the same tail-rendering driver (no echo opt-out).
def test_headless_resume_renders_the_resume_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "should_use_color", lambda *a, **k: False)
    monkeypatch.setattr(m, "_select_headless", lambda args: (True, False))
    monkeypatch.setattr("imp.cli.session_run.make_headless_solicit", lambda *a, **k: None)

    async def _replay_into(quest: object, *, driver: Any, **k: object) -> int:
        for e in [ev("1", "quest_created", quest_id=1, title="t"), task_lead_started("2"),
                  agent_text("3", "Lead-in before the long tool.")]:
            driver.on_event({**e, "_replay": True})
        driver.on_event({"type": "replay_complete"})
        return 0

    monkeypatch.setattr("imp.cli.session_run.run_session_client", _replay_into)

    rc = anyio.run(m._async_cmd_resume, argparse.Namespace(
        token="feat", non_interactive=True, message="go", autonomous=False))

    assert rc == 0
    assert capsys.readouterr().out.count("• Lead-in before the long tool.") == 1

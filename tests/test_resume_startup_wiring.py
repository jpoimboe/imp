# tests/test_resume_startup_wiring.py
from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

import imp.cli.main as m
import imp.cli.signals as sig
from imp.engine.quest import Quest
from imp.engine.state import State
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

    monkeypatch.setattr(m, "LiveDriver", _CapDriver)
    monkeypatch.setattr(m, "LiveStatus", lambda *a, **k: SimpleNamespace())
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
    monkeypatch.setattr(m, "make_interactive_solicit", lambda *a, **k: None)
    captured = _capture_driver(monkeypatch)

    async def _ok(*a: object, **k: object) -> int:
        return 0

    monkeypatch.setattr(m, "run_session_client", _ok)

    async def _fake_loop(control, *, first):
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
    monkeypatch.setattr(m, "make_headless_solicit", lambda *a, **k: None)
    monkeypatch.setattr(m, "random_status_phrase", lambda: "scheming")
    captured = _capture_driver(monkeypatch)

    async def _ok(*a: object, **k: object) -> int:
        return 0

    monkeypatch.setattr(m, "run_session_client", _ok)

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
    monkeypatch.setattr(m, "make_interactive_solicit", lambda *a, **k: None)
    monkeypatch.setattr(m, "make_buffered_solicit", lambda line, inner: None)
    captured = _capture_driver(monkeypatch)

    async def _ok(*a: object, **k: object) -> int:
        return 0

    monkeypatch.setattr(m, "run_session_client", _ok)

    async def _fake_run_inert(*, question, ask_line, launch):
        return await launch("my answer")

    monkeypatch.setattr(m, "run_inert_resume", _fake_run_inert)

    async def _fake_loop(control, *, first):
        return await first({})

    monkeypatch.setattr(m, "_clear_loop", _fake_loop)

    rc = await m._resume_inert(ch, "Pending Q?")

    assert rc == 0
    assert captured["startup_verb"] == "writing plan"       # labeled step → its label
    assert captured["display_width"] is m._display_width     # item 7: inert now passes width
    assert captured["preceded_by_blank"] is True

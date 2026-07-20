from __future__ import annotations

import argparse
import json
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


def _seed(tmp_path: Path, cid: str, create_mode: str) -> Quest:
    """A paused quest home whose persisted create_mode is `create_mode`."""
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="demo", quest_id=cid, base_branch="main")
    meta_path = ch.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["create_mode"] = create_mode
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    st = State.load(ch.state_dir)
    st.status = "paused"
    st.save(ch.state_dir)
    return ch


def _capture_leave_mode(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    async def _cap(*a: object, **k: object) -> int:
        captured.update(k)
        return 0

    monkeypatch.setattr(m, "run_session_client", _cap)
    return captured


def _stub_ui(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(m, "should_use_color", lambda *a, **k: False)
    monkeypatch.setattr(m, "LiveStatus", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(
        m, "LiveDriver",
        lambda *a, **k: SimpleNamespace(
            install_startup_status=lambda: None,
            echo_submitted_reply=lambda _t: None,
            seed_opening_intent=lambda _t: None))
    monkeypatch.setattr(sig, "InterruptCoordinator", lambda *a, **k: SimpleNamespace())


@pytest.mark.anyio
@pytest.mark.parametrize("create_mode", ["chat", "new"])
async def test_run_fresh_session_headless_threads_create_mode(
    monkeypatch: pytest.MonkeyPatch, create_mode: str,
) -> None:
    _stub_ui(monkeypatch)
    monkeypatch.setattr(m, "make_headless_solicit", lambda *a, **k: None)
    captured = _capture_leave_mode(monkeypatch)
    args = m._build_parser().parse_args(["resume", "--headless"])
    quest = SimpleNamespace(
        id="x", title="t", create_mode=create_mode, seed_opening=None)
    rc = await m._run_fresh_session(quest, args=args)
    assert rc == 0
    assert captured["leave_mode"] == create_mode


@pytest.mark.anyio
@pytest.mark.parametrize("create_mode", ["chat", "new"])
async def test_run_fresh_session_interactive_threads_create_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, create_mode: str,
) -> None:
    _stub_ui(monkeypatch)
    monkeypatch.setattr(m, "_select_headless", lambda _a: (False, False))
    monkeypatch.setattr(m, "make_interactive_solicit", lambda *a, **k: None)

    async def _fake_loop(control: object, *, first: object) -> int:
        return await first({})

    monkeypatch.setattr(m, "_clear_loop", _fake_loop)
    captured = _capture_leave_mode(monkeypatch)
    args = m._build_parser().parse_args(["resume"])
    quest = SimpleNamespace(
        id="x", title="t", create_mode=create_mode,
        control_root=tmp_path, seed_opening=None)
    rc = await m._run_fresh_session(quest, args=args)
    assert rc == 0
    assert captured["leave_mode"] == create_mode


@pytest.mark.parametrize("create_mode", ["chat", "new"])
def test_interactive_resume_threads_create_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, create_mode: str,
) -> None:
    _seed(tmp_path, "feat", create_mode)
    monkeypatch.chdir(tmp_path)
    _stub_ui(monkeypatch)
    monkeypatch.setattr(m, "_print_splash_banner", lambda **k: False)
    monkeypatch.setattr(m, "_is_interactive", lambda **k: True)
    monkeypatch.setattr(m, "make_interactive_solicit", lambda *a, **k: None)
    monkeypatch.setattr(m.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(m.sys.stdout, "isatty", lambda: True)
    captured = _capture_leave_mode(monkeypatch)

    async def _fake_loop(control: object, *, first: object) -> int:
        return await first({})

    monkeypatch.setattr(m, "_clear_loop", _fake_loop)
    rc = anyio.run(m._async_cmd_resume, argparse.Namespace(
        token="feat", non_interactive=False, message=None))
    assert rc == 0
    assert captured["leave_mode"] == create_mode


@pytest.mark.parametrize("create_mode", ["chat", "new"])
def test_headless_resume_threads_create_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, create_mode: str,
) -> None:
    _seed(tmp_path, "feat", create_mode)
    monkeypatch.chdir(tmp_path)
    _stub_ui(monkeypatch)
    monkeypatch.setattr(m, "_select_headless", lambda args: (True, False))
    monkeypatch.setattr(m, "make_headless_solicit", lambda *a, **k: None)
    captured = _capture_leave_mode(monkeypatch)
    rc = anyio.run(m._async_cmd_resume, argparse.Namespace(
        token="feat", non_interactive=True, message="go", autonomous=False))
    assert rc == 0
    assert captured["leave_mode"] == create_mode


@pytest.mark.anyio
@pytest.mark.parametrize("create_mode", ["chat", "new"])
async def test_inert_resume_threads_create_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, create_mode: str,
) -> None:
    _seed(tmp_path, "feat", create_mode)
    quest = Quest.load(tmp_path, "feat")
    _stub_ui(monkeypatch)
    monkeypatch.setattr(m, "_term_width", lambda: 80)
    monkeypatch.setattr(m, "_prelaunch_position_model", lambda *a, **k: None)
    monkeypatch.setattr(m, "compose_prelaunch_position_row", lambda **k: "")
    monkeypatch.setattr(m, "render_inert_question", lambda *a, **k: a[0])
    monkeypatch.setattr(m, "make_interactive_solicit", lambda *a, **k: None)
    monkeypatch.setattr(m, "make_buffered_solicit", lambda line, inner: None)
    captured = _capture_leave_mode(monkeypatch)

    async def _fake_run_inert(*, question: object, ask_line: object, launch: object) -> int:
        return await launch("my answer")

    async def _fake_loop(control: object, *, first: object) -> int:
        return await first({})

    monkeypatch.setattr(m, "run_inert_resume", _fake_run_inert)
    monkeypatch.setattr(m, "_clear_loop", _fake_loop)
    rc = await m._resume_inert(
        quest, "the question?", marker="resuming", notice=None, autonomous=False)
    assert rc == 0
    assert captured["leave_mode"] == create_mode

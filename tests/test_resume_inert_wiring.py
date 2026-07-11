from __future__ import annotations

import argparse
from pathlib import Path

import pytest

import imp.cli.main as m
from imp.engine.quest import Quest
from imp.engine.state import State
from tests.conftest import _seed_control


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _args(cid: str) -> argparse.Namespace:
    return argparse.Namespace(token=cid, message=None, non_interactive=False)


def _args_with_message(cid: str, msg: str) -> argparse.Namespace:
    return argparse.Namespace(token=cid, message=msg, non_interactive=False)


def _seed(tmp_path: Path) -> Quest:
    _seed_control(tmp_path)
    ch = Quest.create_draft(
        tmp_path, title="C", quest_id="c", base_branch="main")
    State(id="c", step="s", step_attempt=0, status="paused").save(ch.state_dir)
    return ch


def _patch_common(monkeypatch, tmp_path: Path, ch: Quest) -> None:
    monkeypatch.setattr(m, "install_process_group", lambda: None)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_resolve_id", lambda control, token: "c")
    monkeypatch.setattr(m, "_load_or_rehydrate", lambda control, cid: ch)
    monkeypatch.setattr(m, "_resume_opens_fresh", lambda change: False)
    monkeypatch.setattr(m, "_select_headless", lambda args: (False, False))


@pytest.mark.anyio
async def test_resume_routes_to_inert_for_bare_turn_end(
        monkeypatch, tmp_path: Path) -> None:
    ch = _seed(tmp_path)
    _patch_common(monkeypatch, tmp_path, ch)
    monkeypatch.setattr(m, "bare_turn_end_question", lambda change: "Pending Q?")
    seen: dict[str, str] = {}

    async def fake_inert(change, question, *, autonomous=False):
        seen["question"] = question
        return 0

    monkeypatch.setattr(m, "_resume_inert", fake_inert)

    rc = await m._async_cmd_resume(_args("c"))

    assert rc == 0
    assert seen["question"] == "Pending Q?"


@pytest.mark.anyio
async def test_resume_skips_inert_when_not_bare_turn_end(
        monkeypatch, tmp_path: Path) -> None:
    ch = _seed(tmp_path)
    _patch_common(monkeypatch, tmp_path, ch)
    monkeypatch.setattr(m, "_print_splash_banner", lambda **k: False)
    monkeypatch.setattr(m, "bare_turn_end_question", lambda change: None)

    async def boom_inert(change, question):
        raise AssertionError("inert branch taken for a non-bare-turn-end resume")

    monkeypatch.setattr(m, "_resume_inert", boom_inert)

    async def fake_clear_loop(control_root, *, first):
        return 9

    monkeypatch.setattr(m, "_clear_loop", fake_clear_loop)

    rc = await m._async_cmd_resume(_args("c"))

    assert rc == 9


@pytest.mark.anyio
async def test_resume_skips_inert_when_message_supplied(
        monkeypatch, tmp_path: Path) -> None:
    ch = _seed(tmp_path)
    _patch_common(monkeypatch, tmp_path, ch)
    monkeypatch.setattr(m, "_print_splash_banner", lambda **k: False)
    monkeypatch.setattr(m, "bare_turn_end_question", lambda change: "Pending Q?")

    async def boom_inert_msg(change, question):
        raise AssertionError("inert branch taken despite message being set")

    monkeypatch.setattr(m, "_resume_inert", boom_inert_msg)

    async def fake_clear_loop(control_root, *, first):
        return 7

    monkeypatch.setattr(m, "_clear_loop", fake_clear_loop)

    rc = await m._async_cmd_resume(_args_with_message("c", "hello"))

    assert rc == 7


class _RaisingPrompt:
    def __init__(self, exc: type[BaseException], **kwargs: object) -> None:
        self._exc = exc

    async def ask_async(self, *args: object, **kwargs: object) -> str:
        raise self._exc


@pytest.mark.anyio
async def test_inert_ask_line_maps_eof_to_none(
        monkeypatch, tmp_path: Path) -> None:
    ch = _seed(tmp_path)
    monkeypatch.setattr(
        m, "Prompt", lambda **kwargs: _RaisingPrompt(EOFError, **kwargs))
    assert await m._inert_ask_line(ch) is None


@pytest.mark.anyio
async def test_inert_ask_line_maps_ctrl_c_to_none(
        monkeypatch, tmp_path: Path) -> None:
    ch = _seed(tmp_path)
    monkeypatch.setattr(
        m, "Prompt", lambda **kwargs: _RaisingPrompt(KeyboardInterrupt, **kwargs))
    assert await m._inert_ask_line(ch) is None


@pytest.mark.anyio
async def test_resume_inert_receives_autonomous(
        monkeypatch, tmp_path: Path) -> None:
    ch = _seed(tmp_path)
    _patch_common(monkeypatch, tmp_path, ch)
    monkeypatch.setattr(m, "bare_turn_end_question", lambda change: "Pending Q?")
    seen: dict[str, object] = {}

    async def fake_inert(change, question, *, autonomous=False):
        seen["autonomous"] = autonomous
        return 0

    monkeypatch.setattr(m, "_resume_inert", fake_inert)

    args = argparse.Namespace(
        token="c", message=None, non_interactive=False, autonomous=True)
    rc = await m._async_cmd_resume(args)

    assert rc == 0
    assert seen["autonomous"] is True

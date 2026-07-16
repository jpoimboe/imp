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
    monkeypatch.setattr(m, "_resume_opens_fresh", lambda quest: False)
    monkeypatch.setattr(m, "_select_headless", lambda args: (False, False))


@pytest.mark.anyio
async def test_resume_routes_to_inert_for_bare_turn_end(
        monkeypatch, tmp_path: Path) -> None:
    ch = _seed(tmp_path)
    _patch_common(monkeypatch, tmp_path, ch)
    monkeypatch.setattr(m, "bare_turn_end_question", lambda quest: "Pending Q?")
    seen: dict[str, str] = {}

    async def fake_inert(quest, question, *, autonomous=False):
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
    monkeypatch.setattr(m, "bare_turn_end_question", lambda quest: None)

    async def boom_inert(quest, question):
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
    monkeypatch.setattr(m, "bare_turn_end_question", lambda quest: "Pending Q?")

    async def boom_inert_msg(quest, question):
        raise AssertionError("inert branch taken despite message being set")

    monkeypatch.setattr(m, "_resume_inert", boom_inert_msg)

    async def fake_clear_loop(control_root, *, first):
        return 7

    monkeypatch.setattr(m, "_clear_loop", fake_clear_loop)

    rc = await m._async_cmd_resume(_args_with_message("c", "hello"))

    assert rc == 7


@pytest.mark.anyio
async def test_resume_routes_to_inert_reading_recorded_message(
        monkeypatch, tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(
        tmp_path, title="C", quest_id="c", base_branch="main")
    ch.event_log().append(
        agent_id="step:p/s", type="awaiting_user_input",
        payload={"steer_hint": "", "last_agent_message": "Which DB should I use?",
                 "interrupt": False})
    State(id="c", step="s", step_attempt=0, status="new").save(ch.state_dir)

    # bare_turn_end_question is NOT patched here: the real event-driven decision
    # runs against ch's journaled gate.
    _patch_common(monkeypatch, tmp_path, ch)
    seen: dict[str, str] = {}

    async def fake_inert(quest, question, *, autonomous=False):
        seen["question"] = question
        return 0

    monkeypatch.setattr(m, "_resume_inert", fake_inert)

    rc = await m._async_cmd_resume(_args("c"))

    assert rc == 0
    assert seen["question"] == "Which DB should I use?"


class _CapturingPrompt:
    def __init__(self, sink: dict[str, object], **kwargs: object) -> None:
        self._sink = sink

    async def ask_async(self, *args: object, **kwargs: object) -> str:
        self._sink["bottom_toolbar"] = kwargs.get("bottom_toolbar")
        self._sink["width"] = kwargs.get("width")
        return "go"


@pytest.mark.anyio
async def test_inert_ask_line_passes_identity_toolbar(
        monkeypatch, tmp_path: Path) -> None:
    from imp.cli.live_driver import compose_identity_toolbar

    ch = _seed(tmp_path)
    monkeypatch.setattr(m, "should_use_color", lambda _stream: False)
    monkeypatch.setattr(m, "_term_width", lambda: 80)
    sink: dict[str, object] = {}
    monkeypatch.setattr(
        m, "Prompt", lambda **kwargs: _CapturingPrompt(sink, **kwargs))

    line = await m._inert_ask_line(ch)

    assert line == "go"
    expected = compose_identity_toolbar(
        quest_id="c", title="C", color=False, width=80)
    assert expected is not None
    assert sink["bottom_toolbar"] == expected
    assert sink["width"] == 80


@pytest.mark.anyio
async def test_inert_ask_line_forwards_toolbar_override(
        monkeypatch, tmp_path: Path) -> None:
    # The inert path passes a precomposed position row; the shared reader threads
    # it to ask_async as the bottom toolbar (no re-fork of _read_intent_line).
    ch = _seed(tmp_path)
    monkeypatch.setattr(m, "should_use_color", lambda _stream: False)
    monkeypatch.setattr(m, "_term_width", lambda: 80)
    sink: dict[str, object] = {}
    monkeypatch.setattr(
        m, "Prompt", lambda **kwargs: _CapturingPrompt(sink, **kwargs))

    line = await m._inert_ask_line(ch, toolbar="POSITION-ROW")

    assert line == "go"
    assert sink["bottom_toolbar"] == "POSITION-ROW"


@pytest.mark.anyio
async def test_resume_inert_composes_position_row_for_prompt(
        monkeypatch, tmp_path: Path) -> None:
    ch = _seed(tmp_path)
    captured: dict[str, object] = {}

    async def _cap_ask(quest, *, toolbar=None):
        captured["toolbar"] = toolbar
        return None

    monkeypatch.setattr(m, "should_use_color", lambda _stream: False)
    monkeypatch.setattr(m, "_term_width", lambda: 80)
    monkeypatch.setattr(m, "compose_prelaunch_position_row", lambda **k: "POS-ROW")
    monkeypatch.setattr(m, "_inert_ask_line", _cap_ask)

    async def _fake_run_inert(*, question, ask_line, launch):
        await ask_line()
        return 0

    monkeypatch.setattr(m, "run_inert_resume", _fake_run_inert)

    rc = await m._resume_inert(ch, "Which DB should I use?")

    assert rc == 0
    assert captured["toolbar"] == "POS-ROW"      # the composed row reached the reader


def test_prelaunch_position_model_resolves_step_tier_from_state(
        tmp_path: Path) -> None:
    from imp.providers.config import ProviderModel
    ch = _seed(tmp_path)
    state = State.load(ch.state_dir)
    state.step = "s"                    # a real workflow step (default tier: medium)
    state.resolved_steps = None         # no per-step override → resolve via tier map
    state.resolved_tiers = {
        "medium": ProviderModel(provider="anthropic", model="tier-model")}
    assert m._prelaunch_position_model(ch, state) == "tier-model"


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
    monkeypatch.setattr(m, "bare_turn_end_question", lambda quest: "Pending Q?")
    seen: dict[str, object] = {}

    async def fake_inert(quest, question, *, autonomous=False):
        seen["autonomous"] = autonomous
        return 0

    monkeypatch.setattr(m, "_resume_inert", fake_inert)

    args = argparse.Namespace(
        token="c", message=None, non_interactive=False, autonomous=True)
    rc = await m._async_cmd_resume(args)

    assert rc == 0
    assert seen["autonomous"] is True


class _QueueInertPrompt:
    """Returns queued lines in order from ask_async (accepts the reader's kwargs)."""

    def __init__(self, lines: list[str], **kwargs: object) -> None:
        self._lines = list(lines)

    async def ask_async(self, *args: object, **kwargs: object) -> str:
        return self._lines.pop(0)


@pytest.mark.anyio
async def test_inert_ask_line_rejects_session_verb_and_reprompts(
        monkeypatch, tmp_path: Path, capsys) -> None:
    # A pre-launch session verb (/keep) is rejected with a nudge and re-prompt,
    # exactly as the opening prompt does — only the real line is returned.
    ch = _seed(tmp_path)
    (ch.workflow_dir / "skills" / "user").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(m, "should_use_color", lambda _s: False)
    monkeypatch.setattr(m, "_term_width", lambda: 80)
    monkeypatch.setattr(
        m, "Prompt",
        lambda **kw: _QueueInertPrompt(["/keep", "real answer"], **kw))

    line = await m._inert_ask_line(ch)

    assert line == "real answer"
    assert "/keep" in capsys.readouterr().out   # the nudge explained the rejection


@pytest.mark.anyio
async def test_inert_ask_line_ctrl_d_retains_caret(
        monkeypatch, tmp_path: Path, capsys) -> None:
    # Ctrl-D retains the gold `> ` caret + one trailing blank, prints nothing
    # else, and maps to None (the quest stays parked).
    ch = _seed(tmp_path)
    monkeypatch.setattr(m, "should_use_color", lambda _s: False)
    monkeypatch.setattr(m, "_term_width", lambda: 80)
    monkeypatch.setattr(
        m, "Prompt", lambda **kw: _RaisingPrompt(EOFError, **kw))

    line = await m._inert_ask_line(ch)

    assert line is None
    assert capsys.readouterr().out == "> \n\n"   # caret line, then one blank


@pytest.mark.anyio
@pytest.mark.parametrize("exc", [EOFError, KeyboardInterrupt])
async def test_resume_inert_ctrl_d_retains_caret_parks_and_returns_zero(
    exc: type[BaseException], monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    # AC7: cancel at the inert prompt parks the quest, returns 0, caret kept.
    ch = _seed(tmp_path)
    before = ch.events_path.read_bytes() if ch.events_path.exists() else b""
    monkeypatch.setattr(m, "should_use_color", lambda _s: False)
    monkeypatch.setattr(m, "_term_width", lambda: 80)
    monkeypatch.setattr(m, "Prompt", lambda **kw: _RaisingPrompt(exc, **kw))

    rc = await m._resume_inert(ch, "Which DB should I use?")

    assert rc == 0
    after = ch.events_path.read_bytes() if ch.events_path.exists() else b""
    assert after == before                       # nothing launched -> quest parked
    out = capsys.readouterr().out
    assert "c · C · resuming" in out             # resuming header printed
    assert "Which DB should I use?" in out       # pending question replayed
    assert out.endswith("> \n\n")                # retained caret + one trailing blank
    assert "cancelled" not in out                # no `imp: cancelled` message

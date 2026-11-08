from __future__ import annotations

import argparse
import asyncio
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import imp.cli.main as m
import imp.cli.signals as sig
from imp.cli.exit_codes import ExitCode
from imp.cli.render_summary import build_summary, summary_totals
from imp.engine.quest import Quest
from imp.engine.state import State
from imp.paths import project_state_dir
from imp.providers.quota import QuotaReading, QuotaWindow
from tests._event_fixtures import ev
from tests.async_wait import HANG_BACKSTOP
from tests.conftest import _branch_with_commit, _seed_control


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
    monkeypatch.setattr("imp.cli.signals.install_process_group", lambda: None)
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
    monkeypatch.setattr("imp.cli.inert_resume.bare_turn_end_question", lambda quest: "Pending Q?")
    seen: dict[str, str] = {}

    async def fake_inert(quest, question, *, marker=None, notice=None,
                         autonomous=False, provider=None):
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
    monkeypatch.setattr("imp.cli.inert_resume.bare_turn_end_question", lambda quest: None)

    async def boom_inert(quest, question, *, marker=None, notice=None,
                         autonomous=False, provider=None):
        raise AssertionError("inert branch taken for a non-bare-turn-end resume")

    monkeypatch.setattr(m, "_resume_inert", boom_inert)

    async def fake_clear_loop(control_root, base, *, first):
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
    monkeypatch.setattr("imp.cli.inert_resume.bare_turn_end_question", lambda quest: "Pending Q?")

    async def boom_inert_msg(quest, question, *, marker=None, notice=None,
                             autonomous=False, provider=None):
        raise AssertionError("inert branch taken despite message being set")

    monkeypatch.setattr(m, "_resume_inert", boom_inert_msg)

    async def fake_clear_loop(control_root, base, *, first):
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

    async def fake_inert(quest, question, *, marker=None, notice=None,
                         autonomous=False, provider=None):
        seen["question"] = question
        return 0

    monkeypatch.setattr(m, "_resume_inert", fake_inert)

    rc = await m._async_cmd_resume(_args("c"))

    assert rc == 0
    assert seen["question"] == "Which DB should I use?"


class _CapturingPrompt:
    def __init__(self, sink: dict[str, object], **kwargs: object) -> None:
        self._sink = sink

    def seed_history(self, lines: list[str]) -> None:
        pass

    async def ask_async(self, *args: object, **kwargs: object) -> str:
        self._sink["bottom_toolbar"] = kwargs.get("bottom_toolbar")
        self._sink["width"] = kwargs.get("width")
        self._sink["hint"] = kwargs.get("hint")
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
        quest_id="c", title="C", project=ch.control_root.name,
        color=False, width=80)
    assert expected is not None
    assert sink["bottom_toolbar"] == expected
    assert sink["width"] == 80


@pytest.mark.anyio
async def test_inert_ask_line_keeps_default_hint(
        monkeypatch, tmp_path: Path) -> None:
    ch = _seed(tmp_path)
    monkeypatch.setattr(m, "should_use_color", lambda _stream: False)
    monkeypatch.setattr(m, "_term_width", lambda: 80)
    sink: dict[str, object] = {}
    monkeypatch.setattr(
        m, "Prompt", lambda **kwargs: _CapturingPrompt(sink, **kwargs))

    await m._inert_ask_line(ch)

    assert sink["hint"] == "type a message, or /help"


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
    monkeypatch.setattr("imp.cli.live_driver.compose_prelaunch_position_row", lambda **k: "POS-ROW")
    monkeypatch.setattr(m, "_inert_ask_line", _cap_ask)

    async def _fake_run_inert(*, question, ask_line, launch):
        await ask_line()
        return 0

    monkeypatch.setattr("imp.cli.inert_resume.run_inert_resume", _fake_run_inert)

    rc = await m._resume_inert(ch, "Which DB should I use?", marker="resuming", notice=None)

    assert rc == 0
    assert captured["toolbar"] == "POS-ROW"      # the composed row reached the reader


@pytest.mark.anyio
async def test_resume_inert_prelaunch_row_carries_the_project_name(
        monkeypatch, tmp_path: Path) -> None:
    ch = _seed(tmp_path)
    seen: dict[str, object] = {}

    async def _cap_ask(quest, *, toolbar=None):
        return None

    def _cap_row(**kwargs: object) -> str:
        seen.update(kwargs)
        return "POS-ROW"

    monkeypatch.setattr(m, "should_use_color", lambda _stream: False)
    monkeypatch.setattr(m, "_term_width", lambda: 80)
    monkeypatch.setattr("imp.cli.live_driver.compose_prelaunch_position_row", _cap_row)
    monkeypatch.setattr(m, "_inert_ask_line", _cap_ask)

    async def _fake_run_inert(*, question, ask_line, launch):
        await ask_line()
        return 0

    monkeypatch.setattr("imp.cli.inert_resume.run_inert_resume", _fake_run_inert)

    rc = await m._resume_inert(ch, "Which DB should I use?", marker="resuming",
                               notice=None)

    assert rc == 0
    assert seen["project"] == ch.control_root.name == tmp_path.name


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

    def seed_history(self, lines: list[str]) -> None:
        pass

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
    monkeypatch.setattr("imp.cli.inert_resume.bare_turn_end_question", lambda quest: "Pending Q?")
    seen: dict[str, object] = {}

    async def fake_inert(quest, question, *, marker=None, notice=None,
                         autonomous=False, provider=None):
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

    def seed_history(self, lines: list[str]) -> None:
        pass

    async def ask_async(self, *args: object, **kwargs: object) -> str:
        return self._lines.pop(0)


@pytest.mark.anyio
async def test_inert_ask_line_rejects_session_verb_and_reprompts(
        monkeypatch, tmp_path: Path, capsys) -> None:
    # A pre-launch session verb (/pause) is rejected with the pre-session redirect and a
    # exactly as the opening prompt does — only the real line is returned.
    ch = _seed(tmp_path)
    (ch.workflow_dir / "skills" / "user").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(m, "should_use_color", lambda _s: False)
    monkeypatch.setattr(m, "_term_width", lambda: 80)
    monkeypatch.setattr(
        m, "Prompt",
        lambda **kw: _QueueInertPrompt(["/pause", "real answer"], **kw))

    line = await m._inert_ask_line(ch)

    assert line == "real answer"
    from imp.cli.slash import PRE_SESSION_LEAVE_REDIRECT
    assert PRE_SESSION_LEAVE_REDIRECT in " ".join(capsys.readouterr().out.split())


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
async def test_resume_inert_ctrl_d_retains_caret_parks_and_exits_130(
    exc: type[BaseException], monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    # Ctrl-D at the inert prompt parks the quest and exits 130, caret kept.
    ch = _seed(tmp_path)
    before = ch.events_path.read_bytes() if ch.events_path.exists() else b""
    monkeypatch.setattr(m, "should_use_color", lambda _s: False)
    monkeypatch.setattr(m, "_term_width", lambda: 80)
    monkeypatch.setattr(m, "Prompt", lambda **kw: _RaisingPrompt(exc, **kw))

    rc = await m._resume_inert(ch, "Which DB should I use?", marker="resuming", notice=None)

    assert rc == 130
    after = ch.events_path.read_bytes() if ch.events_path.exists() else b""
    assert after == before                       # nothing launched -> quest parked
    out = capsys.readouterr().out
    assert "c · C · resuming" in out             # resuming header printed
    assert "Which DB should I use?" in out       # pending question replayed
    assert out.endswith("> \n\n")                # retained caret + one trailing blank
    assert "cancelled" not in out                # no `imp: cancelled` message


class _SignalledInertPrompt:
    """Sends `leave_sig` to this process while blocked at the prompt."""

    def __init__(self, leave_sig: int, **kwargs: object) -> None:
        self._sig = leave_sig

    def seed_history(self, lines: list[str]) -> None:
        pass

    async def ask_async(self, *args: object, **kwargs: object) -> str:
        os.kill(os.getpid(), self._sig)
        await asyncio.Event().wait()
        raise AssertionError("the leave signal must cancel the blocked prompt")


_LEAVE_SIGNALS = pytest.mark.parametrize(
    "leave_sig,display", [(signal.SIGTERM, "SIGTERM"), (signal.SIGHUP, "SIGHUP")],
    ids=["sigterm", "sighup"])


@pytest.mark.anyio
@_LEAVE_SIGNALS
async def test_inert_ask_line_leave_signal_prints_pause_block_without_caret(
    leave_sig: int, display: str, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
    signal_sentinels: Callable[[int, object], None],
) -> None:
    # A signal leave at the inert prompt prints the standard signal-leave block
    # (no caret: nothing was typed) and maps to None like Ctrl-D.
    ch = _seed(tmp_path)
    monkeypatch.setattr(m, "should_use_color", lambda _s: False)
    monkeypatch.setattr(m, "_term_width", lambda: 80)
    monkeypatch.setattr(
        m, "Prompt", lambda **kw: _SignalledInertPrompt(leave_sig, **kw))

    async with asyncio.timeout(HANG_BACKSTOP):
        line = await m._inert_ask_line(ch)

    assert line is None
    assert capsys.readouterr().out == (
        f"paused · {display}\nc · C\nresume: imp resume c\n\n")


@pytest.mark.anyio
async def test_inert_ask_line_leave_signal_names_a_titleless_quest_by_its_opening(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    signal_sentinels: Callable[[int, object], None],
) -> None:
    # A titleless saved chat: the identity line carries the opening, not a bare id.
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="", quest_id="c", base_branch="main")
    State(id="c", step="s", step_attempt=0, status="paused").save(ch.state_dir)
    ch.event_log().append(
        agent_id=None, type="user_message",
        payload={"subtype": "intent",
                 "content": [{"type": "text", "text": "summarize foo.py"}]})
    monkeypatch.setattr(m, "should_use_color", lambda _s: False)
    monkeypatch.setattr(m, "_term_width", lambda: 80)
    monkeypatch.setattr(
        m, "Prompt", lambda **kw: _SignalledInertPrompt(signal.SIGTERM, **kw))

    async with asyncio.timeout(HANG_BACKSTOP):
        line = await m._inert_ask_line(ch)

    assert line is None
    assert capsys.readouterr().out == (
        "paused · SIGTERM\nc · summarize foo.py\nresume: imp resume c\n\n")


@pytest.mark.anyio
@_LEAVE_SIGNALS
async def test_resume_inert_leave_signal_parks_and_exits_130(
    leave_sig: int, display: str, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
    signal_sentinels: Callable[[int, object], None],
) -> None:
    # Through the whole inert-resume flow: rc 130, nothing launched or journaled,
    # the quest still parked, and the pause block is the last thing printed.
    ch = _seed(tmp_path)
    before = ch.events_path.read_bytes() if ch.events_path.exists() else b""
    monkeypatch.setattr(m, "should_use_color", lambda _s: False)
    monkeypatch.setattr(m, "_term_width", lambda: 80)
    monkeypatch.setattr(
        m, "Prompt", lambda **kw: _SignalledInertPrompt(leave_sig, **kw))

    async with asyncio.timeout(HANG_BACKSTOP):
        rc = await m._resume_inert(
            ch, "Which DB should I use?", marker="resuming", notice=None)

    assert rc == 130
    after = ch.events_path.read_bytes() if ch.events_path.exists() else b""
    assert after == before
    assert State.load(ch.state_dir).status == "paused"
    out = capsys.readouterr().out
    assert out.endswith(f"paused · {display}\nc · C\nresume: imp resume c\n\n")
    assert not any(ln.startswith(">") for ln in out.splitlines())   # no caret line


@pytest.mark.anyio
async def test_inert_ask_line_seeds_history_from_event_log(
        monkeypatch, tmp_path: Path) -> None:
    # Up-arrow recalls prior HUMAN turns; synthetic turns are excluded.
    ch = _seed(tmp_path)
    ch.event_log().append(
        agent_id="step:p/s", type="user_message",
        payload={"role": "user", "subtype": "reply",
                 "content": [{"type": "text", "text": "a human reply"}]})
    ch.event_log().append(
        agent_id="step:p/s", type="engine_message",
        payload={"role": "user", "subtype": "continuation",
                 "content": [{"type": "text", "text": "engine nudge"}]})
    seeded: dict[str, object] = {}

    class _RecordingPrompt:
        def __init__(self, **kwargs: object) -> None:
            pass

        def seed_history(self, lines: list[str]) -> None:
            seeded["lines"] = lines

        async def ask_async(self, *args: object, **kwargs: object) -> str:
            return "go"

    monkeypatch.setattr(m, "should_use_color", lambda _stream: False)
    monkeypatch.setattr(m, "_term_width", lambda: 80)
    monkeypatch.setattr(m, "Prompt", lambda **kwargs: _RecordingPrompt(**kwargs))

    line = await m._inert_ask_line(ch)

    assert line == "go"
    assert seeded["lines"] == ["a human reply"]      # synthetic turn excluded


@pytest.mark.anyio
async def test_inert_resume_echoes_reply_before_session_boot(
        monkeypatch, tmp_path: Path) -> None:
    ch = _seed(tmp_path)
    echoed: list[str] = []
    seen_at_boot: dict[str, list[str]] = {}

    class _EchoDriver:
        def __init__(self, **kwargs: object) -> None:
            pass

        def echo_submitted_reply(self, text: str) -> None:
            echoed.append(text)

        def install_startup_status(self) -> None:
            pass

    monkeypatch.setattr(m, "should_use_color", lambda _stream: False)
    monkeypatch.setattr(m, "_build_resume_driver", lambda **k: _EchoDriver())
    monkeypatch.setattr("imp.cli.live_status.LiveStatus", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(sig, "InterruptCoordinator", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr("imp.cli.session_run.make_interactive_solicit", lambda *a, **k: None)
    monkeypatch.setattr("imp.cli.inert_resume.make_buffered_solicit", lambda line, inner: None)

    async def _client(*a: object, **k: object) -> int:
        seen_at_boot["echoed"] = list(echoed)      # snapshot when boot begins
        return 0

    monkeypatch.setattr("imp.cli.session_run.run_session_client", _client)

    async def _fake_run_inert(*, question, ask_line, launch):
        return await launch("my answer")

    monkeypatch.setattr("imp.cli.inert_resume.run_inert_resume", _fake_run_inert)

    async def _fake_loop(control, base, *, first):
        return await first({})

    monkeypatch.setattr(m, "_clear_loop", _fake_loop)

    rc = await m._resume_inert(ch, "Which DB should I use?", marker="resuming", notice=None)

    assert rc == 0
    assert echoed == ["my answer"]                 # echoed at submit
    assert seen_at_boot["echoed"] == ["my answer"] # BEFORE run_session_client ran


@pytest.mark.anyio
async def test_inert_resume_carries_idle_terminal_title_into_boot(
        monkeypatch, tmp_path: Path) -> None:
    from imp.cli.terminal_title import TerminalTitle
    ch = _seed(tmp_path)
    seen: dict[str, object] = {}

    class _Drv:
        def __init__(self, **kwargs: object) -> None:
            pass

        def echo_submitted_reply(self, text: str) -> None:
            pass

        def install_startup_status(self) -> None:
            pass

    monkeypatch.setattr(m, "should_use_color", lambda _stream: False)
    monkeypatch.setattr(m, "_build_resume_driver", lambda **k: _Drv())
    monkeypatch.setattr("imp.cli.live_status.LiveStatus", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(sig, "InterruptCoordinator", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr("imp.cli.session_run.make_interactive_solicit", lambda *a, **k: None)
    monkeypatch.setattr("imp.cli.inert_resume.make_buffered_solicit", lambda line, inner: None)

    async def _client(*a: object, title: object = None, **k: object) -> int:
        seen["title"] = title
        return 0

    monkeypatch.setattr("imp.cli.session_run.run_session_client", _client)

    async def _fake_run_inert(*, question, ask_line, launch):
        return await launch("my answer")

    monkeypatch.setattr("imp.cli.inert_resume.run_inert_resume", _fake_run_inert)

    async def _fake_loop(control, base, *, first):
        return await first({})

    monkeypatch.setattr(m, "_clear_loop", _fake_loop)

    rc = await m._resume_inert(ch, "Which DB should I use?", marker="resuming", notice=None)

    assert rc == 0
    assert isinstance(seen["title"], TerminalTitle)
    assert seen["title"]._idle is True        # created idle for the wait
    assert seen["title"]._project == ch.control_root.name == tmp_path.name


@pytest.mark.anyio
async def test_resume_inert_forwards_provider_to_session_client(
        monkeypatch, tmp_path: Path) -> None:
    ch = _seed(tmp_path)
    seen: dict[str, object] = {}

    class _Drv:
        def __init__(self, **kwargs: object) -> None:
            pass

        def echo_submitted_reply(self, text: str) -> None:
            pass

        def install_startup_status(self) -> None:
            pass

    monkeypatch.setattr(m, "should_use_color", lambda _stream: False)
    monkeypatch.setattr(m, "_build_resume_driver", lambda **k: _Drv())
    monkeypatch.setattr("imp.cli.live_status.LiveStatus", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(sig, "InterruptCoordinator", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr("imp.cli.session_run.make_interactive_solicit", lambda *a, **k: None)
    monkeypatch.setattr("imp.cli.inert_resume.make_buffered_solicit", lambda line, inner: None)

    async def _client(*a: object, provider: object = None, **k: object) -> int:
        seen["provider"] = provider
        return 0

    monkeypatch.setattr("imp.cli.session_run.run_session_client", _client)

    async def _fake_run_inert(*, question, ask_line, launch):
        return await launch("my answer")

    monkeypatch.setattr("imp.cli.inert_resume.run_inert_resume", _fake_run_inert)

    async def _fake_loop(control, base, *, first):
        return await first({})

    monkeypatch.setattr(m, "_clear_loop", _fake_loop)

    rc = await m._resume_inert(ch, "Which DB?", marker="resuming", notice=None,
                               provider="codex")

    assert rc == 0
    assert seen["provider"] == "codex"


@pytest.mark.anyio
async def test_inert_resume_hands_quest_base_to_clear_loop(
        monkeypatch, tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _branch_with_commit(tmp_path, "topic")     # leaves the checkout on main
    ch = Quest.create_draft(
        tmp_path, title="C", quest_id="c2", base_branch="topic")
    State(id="c2", step="s", step_attempt=0, status="paused").save(ch.state_dir)
    seen: dict[str, object] = {}

    async def _fake_loop(control, base, *, first):
        seen["base"] = base
        return 0

    monkeypatch.setattr(m, "_clear_loop", _fake_loop)
    monkeypatch.setattr(m, "should_use_color", lambda _stream: False)
    monkeypatch.setattr(m, "_term_width", lambda: 80)
    monkeypatch.setattr(m, "_prelaunch_position_model", lambda *a, **k: None)
    monkeypatch.setattr("imp.cli.live_driver.compose_prelaunch_position_row", lambda **k: "")
    monkeypatch.setattr("imp.cli.inert_resume.render_inert_question", lambda *a, **k: a[0])

    async def _fake_run_inert(*, question, ask_line, launch):
        return await launch("my answer")

    monkeypatch.setattr("imp.cli.inert_resume.run_inert_resume", _fake_run_inert)

    rc = await m._resume_inert(ch, "Which DB?", marker="resuming", notice=None)

    assert rc == 0
    assert seen["base"] == "topic"          # quest.base_branch, not the checkout


_WINDOWS = "session 62% (3h left) · week 7% (5d left)"


def _log(ch: Quest, *events: dict[str, Any]) -> list[dict[str, Any]]:
    """Journal `events` into the quest's real log; return the log as read back."""
    log = ch.event_log()
    for e in events:
        log.append(agent_id=e["agent_id"], type=e["type"], payload=e["payload"])
    return list(ch.event_log().read())


def _future_reading() -> QuotaReading:
    """Resets 3h and ~5d out, sat mid-bucket: 30 minutes of margin before the
    session window would coarsen to `2h` and ~11h before the weekly one reads
    `4d`, so a run stalled under load still renders the same countdown."""
    now = int(time.time())
    return QuotaReading(provider="claude", plan=None,
                        windows=(QuotaWindow("5h", 62.0, now + 10_800),
                                 QuotaWindow("7d", 7.0, now + 430_000)),
                        raw={})


def _cc_turn(**payload: Any) -> dict[str, Any]:
    """A step-level claude turn. Its usage prices nothing, so any dollars on
    the row could only have come from somewhere else."""
    return ev("m1", "agent_message", agent_id="step:p/s", provider="claude",
              model="claude-opus-5", stop_reason="end_turn",
              raw={"content": [{"type": "text", "text": "done"}]}, usage={},
              **payload)


async def _inert_toolbar(monkeypatch: pytest.MonkeyPatch, ch: Quest, *,
                         width: int = 200) -> str:
    """Run _resume_inert far enough to capture the prelaunch row it composed."""
    captured: dict[str, object] = {}

    async def _cap_ask(quest: Quest, *, toolbar: str | None = None) -> None:
        captured["toolbar"] = toolbar
        return None

    async def _fake_run_inert(*, question: str, ask_line: Any,
                              launch: Any) -> int:
        await ask_line()
        return 0

    monkeypatch.setattr(m, "should_use_color", lambda _stream: False)
    monkeypatch.setattr(m, "_term_width", lambda: width)
    monkeypatch.setattr(m, "_inert_ask_line", _cap_ask)
    monkeypatch.setattr("imp.cli.inert_resume.run_inert_resume", _fake_run_inert)

    rc = await m._resume_inert(ch, "Which DB should I use?", marker="resuming",
                               notice=None)
    assert rc == 0
    row = captured["toolbar"]
    assert isinstance(row, str)
    return row


# AC 10(a)
@pytest.mark.anyio
async def test_the_inert_row_carries_the_logged_providers_quota(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ch = _seed(tmp_path)
    reading = _future_reading()
    _log(ch, _cc_turn(auth_mode="subscription"),
         ev("q1", "quota_reading", agent_id="step:p/s", **reading.to_payload()))

    row = await _inert_toolbar(monkeypatch, ch)

    assert row.startswith(f"{tmp_path.name} · c · C")   # left group unchanged
    assert row.endswith(f"claude · {_WINDOWS}")


# AC 10(b)
@pytest.mark.anyio
async def test_the_inert_row_carries_the_quests_dollars_when_metered(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ch = _seed(tmp_path)
    events = _log(
        ch,
        ev("s1", "step_started", step="s", kind="agent", phase="p",
           model="claude-opus-5", provider="anthropic"),
        ev("m1", "agent_message", agent_id="step:p/s", provider="anthropic",
           model="claude-opus-5", stop_reason="end_turn",
           raw={"content": [{"type": "text", "text": "done"}]},
           usage={"input_tokens": 120000, "output_tokens": 9000}))
    total = summary_totals(build_summary(events), None)[1]
    assert total.priced == 1 and total.dollars > 0   # anthropic states no auth_mode

    row = await _inert_toolbar(monkeypatch, ch)

    # The parked row and the summary report one figure, by one computation.
    assert row.endswith(f" ${total.dollars:.2f}")
    assert "session" not in row and "anthropic" not in row


# AC 10(c), AC 11
@pytest.mark.anyio
async def test_the_inert_row_stays_bare_without_a_reading_or_a_priced_turn(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ch = _seed(tmp_path)
    bare = f"{tmp_path.name} · c · C"
    # A log holding neither a turn nor a reading: today's row, byte for byte.
    assert await _inert_toolbar(monkeypatch, ch) == bare

    # A claude turn prices nothing and states no reading: a carrier now
    # exists, and there is still no group and no placeholder standing in for it.
    _log(ch, _cc_turn(auth_mode="subscription"))
    row = await _inert_toolbar(monkeypatch, ch)
    assert row == bare
    assert "n/a" not in row and "—" not in row


def _seed_built(tmp_path: Path) -> Quest:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="C", quest_id="c", base_branch="main")
    (ch.workflow_dir / "skills" / "user").mkdir(parents=True, exist_ok=True)
    return ch


def _fake_confirm(monkeypatch, answer: bool) -> list[str]:
    asked: list[str] = []

    async def _confirm(question: str) -> bool:
        asked.append(question)
        return answer

    monkeypatch.setattr("imp.cli.abandon_confirm.ask_keypress_confirm", _confirm)

    def _no_input(prompt: str = "") -> str:
        raise AssertionError("inert /abandon must use the keypress confirm, not input()")

    monkeypatch.setattr("builtins.input", _no_input)
    return asked


_C_QUESTION = ('Abandon "c · C"? This removes its worktree and branch imp/c (no commits). '
               "[y/N] ")


@pytest.mark.anyio
async def test_inert_abandon_built_confirm_tears_down(
        monkeypatch, tmp_path: Path, capsys) -> None:
    ch = _seed_built(tmp_path)
    monkeypatch.setattr(m, "should_use_color", lambda _s: False)
    monkeypatch.setattr(m, "_term_width", lambda: 80)
    monkeypatch.setattr(m, "Prompt", lambda **kw: _QueueInertPrompt(["/abandon"], **kw))
    asked = _fake_confirm(monkeypatch, True)

    with pytest.raises(SystemExit) as excinfo:
        await m._inert_ask_line(ch)

    assert excinfo.value.code == ExitCode.USER_ENDED
    assert asked == [_C_QUESTION]
    assert capsys.readouterr().out == (
        "> /abandon\n"
        "Abandoned c · removed worktree and branch imp/c (no commits).\n")
    assert not (project_state_dir(tmp_path) / "worktrees" / "c").exists()


@pytest.mark.anyio
async def test_inert_abandon_built_decline_keeps_and_reasks(
        monkeypatch, tmp_path: Path, capsys) -> None:
    ch = _seed_built(tmp_path)
    monkeypatch.setattr(m, "should_use_color", lambda _s: False)
    monkeypatch.setattr(m, "_term_width", lambda: 80)
    monkeypatch.setattr(
        m, "Prompt", lambda **kw: _QueueInertPrompt(["/abandon", "real answer"], **kw))
    asked = _fake_confirm(monkeypatch, False)

    line = await m._inert_ask_line(ch)

    assert line == "real answer"          # the > line was asked again
    assert asked == [_C_QUESTION]
    assert capsys.readouterr().out == "> /abandon\nKept c.\n\n"
    assert (project_state_dir(tmp_path) / "worktrees" / "c").exists()
    assert (project_state_dir(tmp_path) / "quests" / "c").exists()


@pytest.mark.anyio
async def test_inert_abandon_draft_never_confirms(
        monkeypatch, tmp_path: Path, capsys) -> None:
    ch = _seed(tmp_path)
    (ch.workflow_dir / "skills" / "user").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(m, "should_use_color", lambda _s: False)
    monkeypatch.setattr(m, "_term_width", lambda: 80)
    monkeypatch.setattr(m, "Prompt", lambda **kw: _QueueInertPrompt(["/abandon"], **kw))
    asked = _fake_confirm(monkeypatch, False)
    torn: list[str] = []
    monkeypatch.setattr(m, "_abandon_local",
                        lambda control, qid: torn.append(qid) or 0)

    with pytest.raises(SystemExit) as excinfo:
        await m._inert_ask_line(ch)

    assert excinfo.value.code == ExitCode.USER_ENDED
    assert asked == []
    assert torn == ["c"]
    assert capsys.readouterr().out == ""

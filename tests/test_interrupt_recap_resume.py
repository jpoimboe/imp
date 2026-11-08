"""Resume recap: the dim `was:` line printed at the first qualifying interrupt gate."""
from __future__ import annotations

import io
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.cli import live_driver as ld
from imp.cli.inert_resume import render_inert_question
from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.cli.session_terminal import run_terminal_client
from imp.cli.signals import InterruptCoordinator
from imp.remote.session.client import SessionClient
from imp.remote.session.protocol import encode_event
from tests._event_fixtures import _TTY, agent_text, ev
from tests._session_fakes import serve_raw_frames
from tests.async_wait import HANG_BACKSTOP
from tests.test_narrative_interrupt_recap import (
    LEAD,
    LIVE_GATE,
    NOW,
    SCRIPT_HINT,
    SPEC_REVIEW,
    THRESHOLD,
    Event,
    _ctrl_c,
    _cut_off_then_resume,
    _dispatch,
    _gate,
    _lead_cut_off_mid,
    _leave_and_resume,
    _opening_explorer,
    _opening_thinking,
    _saying,
    _spec_review,
    _sub_work,
    _task_lead,
    _timed,
    _two_cut_off,
)
from tests.test_resume_startup_spinner import Screen, _trimmed

BOUNDARY: Event = {"type": "replay_complete"}
CODER_RECAP = "was: task 1/2 · writing code"
PROSE = "Checking criterion 4."
BLOCK = "• Checking criterion 4."
SPEC_RECAP_AGED = "was: reviewing spec · paused 3m ago"


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def _replayed(events: list[Event]) -> list[Event]:
    return [{**e, "_replay": True} for e in events]


def _resume_driver(out: io.StringIO, *, color: bool = False) -> LiveDriver:
    return LiveDriver(stream=out, color=color, width=80, status=LiveStatus(out),
                      suppress_replay=True, preceded_by_blank=True)


def _two_folds_one_still_running() -> list[Event]:
    """Coder cut off and closed; a later explorer still running blocks the replayed walk."""
    return [_task_lead(), _dispatch("d1", LEAD, "coder", "writing code"), _sub_work("d1"),
            ev("r-d1", "tool_result", LEAD, id="blk-d1", content="stopped",
               kind="interrupted", error=True, id_ns="block"),
            _dispatch("d2", LEAD, "explorer", "exploring codebase"), _sub_work("d2"),
            agent_text("lt0", "Replayed lead prose.", agent_id=LEAD)]


def test_recap_leaves_replay_suppressed_and_live_emission_normal() -> None:
    out = _Pipe()
    drv = _resume_driver(out)
    for e in _replayed(_two_folds_one_still_running()):
        drv.on_event(e)
    drv.on_event(BOUNDARY)
    drv.print_interrupt_recap()
    drv.on_event(ev("r-d2", "tool_result", LEAD, id="blk-d2", content="done",
                    id_ns="block"))         # unblocks the replayed walk: stays hidden
    recap = "• Replayed lead prose.\n\n" + CODER_RECAP + "\n"
    assert out.getvalue() == recap
    drv.on_event(agent_text("lt", "Live prose after the recap.", agent_id=LEAD))
    assert out.getvalue() == recap + "\n• Live prose after the recap.\n"


def test_recap_before_the_replay_boundary_prints_nothing() -> None:
    out = _Pipe()
    drv = _resume_driver(out)
    for e in _replayed(_saying(_cut_off_then_resume("coder", "writing code"), LEAD, PROSE)):
        drv.on_event(e)
    drv.print_interrupt_recap()
    assert out.getvalue() == ""


def test_recap_is_dropped_not_deferred_when_a_prompt_is_already_up() -> None:
    out = _Pipe()
    drv = _resume_driver(out)
    events = _saying(_cut_off_then_resume("coder", "writing code"), LEAD, PROSE)
    for e in [*_replayed(events), BOUNDARY]:
        drv.on_event(e)
    drv.prepare_for_prompt()
    drv.print_interrupt_recap()
    drv.flush_deferred()
    drv.print_interrupt_recap()             # once per attach, even if the first was dropped
    assert "was:" not in out.getvalue()
    assert PROSE not in out.getvalue()


def _prompt_screen(replayed: list[Event], live: list[Event], *, splash: bool = True,
                   recap: bool = True, width: int = 80) -> list[str]:
    """Opening, replay, live events, recap, then the solicit's suspend + prepare + caret."""
    out = _TTY()
    out.write("banner\n\n" if splash else "feat · demo · resuming\n")
    status = LiveStatus(out)
    status._width_fn = lambda: 80
    drv = LiveDriver(stream=out, color=False, width=width, status=status,
                     suppress_replay=True, preceded_by_blank=splash, startup_verb="resuming")
    drv.install_startup_status()
    for e in [*_replayed(replayed), BOUNDARY, *live]:
        drv.on_event(e)
    if recap:
        drv.print_interrupt_recap()
    with status.suspend():
        drv.prepare_for_prompt()
        out.write("> PROMPT")
        screen = Screen()
        screen.feed(out.getvalue())
    return _trimmed(screen)


def test_recap_sits_one_blank_below_the_banner_and_above_the_prompt() -> None:
    lines = _prompt_screen(_cut_off_then_resume("coder", "writing code"), [LIVE_GATE])
    assert lines == ["banner", "", CODER_RECAP, "", "> PROMPT"]


def test_quiet_attach_recap_without_splash_keeps_one_blank_each_side() -> None:
    running = [_task_lead(), _dispatch("d1", LEAD, "coder", "writing code"),
               _sub_work("d1")]
    lines = _prompt_screen(running, _ctrl_c("a", LEAD, "sub:d1"), splash=False)
    assert lines == ["feat · demo · resuming", "", CODER_RECAP, "", "> PROMPT"]


def test_no_recap_text_leaves_the_resume_screen_unchanged() -> None:
    events = _opening_thinking()
    lines = _prompt_screen(events, [LIVE_GATE], recap=False)
    assert lines == ["banner", "", "> PROMPT"]
    assert _prompt_screen(events, [LIVE_GATE], recap=True) == lines


def test_first_prompt_without_a_splash_keeps_its_separator_under_the_header() -> None:
    lines = _prompt_screen(_opening_thinking(), [LIVE_GATE], splash=False, recap=False)
    assert lines == ["feat · demo · resuming", "", "> PROMPT"]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _no_confirm(_q: str) -> bool:
    raise AssertionError("confirm must not be called")


async def _client_output(tmp_path: Path, frames: list[Event], *, color: bool = False) -> str:
    """Stream `frames` through a real SessionClient into run_terminal_client's _on_event."""
    sock = str(tmp_path / "s.sock")
    out = _Pipe()
    driver = _resume_driver(out, color=color)
    payload = b"".join(encode_event(f).encode("utf-8") + b"\n" for f in frames)

    async def _attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    with anyio.fail_after(HANG_BACKSTOP):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await serve_raw_frames(sock, payload, tg)
            await run_terminal_client(
                attach=_attach, driver=driver, coord=InterruptCoordinator(),
                solicit=_solicit, confirm=_no_confirm, leave_mode="new")
            tg.cancel_scope.cancel()
    return out.getvalue()


def _resumed_at(events: list[Event], *live: Event) -> list[Event]:
    return [*_replayed(events), BOUNDARY, *(live or (LIVE_GATE,))]


@pytest.mark.anyio
@pytest.mark.parametrize(("events", "live", "expected"), [
    pytest.param(_cut_off_then_resume("coder", "writing code"), LIVE_GATE,
                 CODER_RECAP, id="task-coder"),
    pytest.param(_cut_off_then_resume("code-reviewer", "reviewing code"), LIVE_GATE,
                 "was: task 1/2 · reviewing code", id="task-code-reviewer"),
    pytest.param(_two_cut_off(), LIVE_GATE,
                 "was: task 1/2 · running 2 subagents", id="task-two-subagents"),
    pytest.param(_lead_cut_off_mid(ev("t1", "thinking", LEAD, content="hmm")), LIVE_GATE,
                 "was: task 1/2", id="task-mid-thinking"),
    pytest.param(_lead_cut_off_mid(ev("t1", "tool_call", LEAD, id="tc1", name="bash",
                                      input={"command": "make check"})), LIVE_GATE,
                 "was: task 1/2", id="task-mid-tool"),
    pytest.param(_spec_review(), LIVE_GATE, "was: reviewing spec", id="spec-review"),
    pytest.param([ev("s1", "step_started", step="verify", kind="script", phase="implement",
                     label="running tests", command="make check"), *_leave_and_resume("a")],
                 _gate("live", None, SCRIPT_HINT), "was: running tests", id="verify-gate"),
    pytest.param(_opening_explorer(), LIVE_GATE, "was: exploring codebase",
                 id="opening-explorer"),
    pytest.param([_task_lead(), _dispatch("d1", LEAD, "coder", "writing code"),
                  _sub_work("d1"), *_leave_and_resume("a")],
                 LIVE_GATE, CODER_RECAP, id="pause-finalize-only"),
    pytest.param([_task_lead(), _dispatch("d1", LEAD, "coder", "writing code"),
                  _sub_work("d1"),
                  ev("r-d1", "tool_result", LEAD, id="blk-d1", content="stopped",
                     kind="interrupted", error=True, id_ns="block")],
                 LIVE_GATE, CODER_RECAP, id="interrupted-tool-result-only"),
])
async def test_resumed_hold_gate_prints_the_recap(
    tmp_path: Path, events: list[Event], live: Event, expected: str,
) -> None:
    assert await _client_output(tmp_path, _resumed_at(events, live)) == expected + "\n"


@pytest.mark.anyio
async def test_nothing_cut_off_in_opening_conversation_prints_nothing(tmp_path: Path) -> None:
    frames = _resumed_at(_saying(_opening_thinking(), THRESHOLD, PROSE))
    assert await _client_output(tmp_path, frames) == ""


@pytest.mark.anyio
async def test_recap_is_dim_with_color_on(tmp_path: Path) -> None:
    out = await _client_output(
        tmp_path, _resumed_at(_cut_off_then_resume("coder", "writing code")), color=True)
    assert out == f"\x1b[2m{CODER_RECAP}\x1b[0m\n"


def _parked_at_steer_gate() -> list[Event]:
    """A live session parked at the task-lead's inline steer gate (no leave yet)."""
    return [_task_lead(), _dispatch("d1", LEAD, "coder", "writing code"), _sub_work("d1"),
            *_ctrl_c("a", LEAD, "sub:d1")]


@pytest.mark.anyio
async def test_attach_to_parked_steer_gate_prints_the_recap(tmp_path: Path) -> None:
    resent = _gate(f"a-g-{LEAD}", LEAD)            # the pending gate, re-sent untagged
    frames = [*_replayed(_parked_at_steer_gate()), BOUNDARY, resent]
    assert await _client_output(tmp_path, frames) == CODER_RECAP + "\n"


@pytest.mark.anyio
async def test_quiet_attach_ctrl_c_recaps_once_at_the_step_agents_gate(
    tmp_path: Path,
) -> None:
    running = [_task_lead(), _dispatch("d1", LEAD, "coder", "writing code"),
               _sub_work("d1")]
    frames = [*_replayed(running), BOUNDARY, *_ctrl_c("a", LEAD, "sub:d1")]
    assert await _client_output(tmp_path, frames) == CODER_RECAP + "\n"


@pytest.mark.anyio
async def test_subagent_gate_alone_prints_nothing(tmp_path: Path) -> None:
    frames = _resumed_at(_saying(_cut_off_then_resume("coder", "writing code"), LEAD, PROSE),
                         _gate("sg", "sub:d1"))
    assert await _client_output(tmp_path, frames) == ""


@pytest.mark.anyio
async def test_no_recap_once_a_live_node_has_emitted(tmp_path: Path) -> None:
    live_text = agent_text("lt", "Looking at the failing test.", agent_id=LEAD)
    frames = _resumed_at(_saying(_cut_off_then_resume("coder", "writing code"), LEAD, PROSE),
                         live_text, LIVE_GATE)
    out = await _client_output(tmp_path, frames)
    assert "Looking at the failing test." in out
    assert "was:" not in out
    assert PROSE not in out


@pytest.mark.anyio
async def test_second_interrupt_gate_prints_no_second_recap(tmp_path: Path) -> None:
    frames = _resumed_at(_saying(_cut_off_then_resume("coder", "writing code"), LEAD, PROSE),
                         LIVE_GATE, _gate("live2", None))
    assert await _client_output(tmp_path, frames) == f"{BLOCK}\n\n{CODER_RECAP}\n"


@pytest.mark.anyio
async def test_question_gate_prints_no_recap(tmp_path: Path) -> None:
    question = ev("q", "awaiting_user_input", LEAD, steer_hint="",
                  last_agent_message="Which file?", interrupt=False)
    frames = _resumed_at(_saying(_cut_off_then_resume("coder", "writing code"), LEAD, PROSE),
                         question)
    assert await _client_output(tmp_path, frames) == ""


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the driver's wall clock to NOW (T0 + 180 s)."""

    class _Clock:
        @staticmethod
        def now(tz: Any = None) -> datetime:
            return datetime.fromisoformat(NOW)

    monkeypatch.setattr(ld, "datetime", _Clock)


@pytest.mark.anyio
@pytest.mark.usefixtures("frozen_now")
async def test_resumed_hold_gate_recap_shows_the_pause_age(tmp_path: Path) -> None:
    frames = _resumed_at(_timed(_cut_off_then_resume("coder", "writing code")))
    assert await _client_output(tmp_path, frames) == CODER_RECAP + " · paused 3m ago\n"


_PRIOR_RESUME = _timed(_leave_and_resume("p"))


# AC5(e)
@pytest.mark.anyio
@pytest.mark.usefixtures("frozen_now")
async def test_parked_steer_gate_attach_after_a_resume_shows_no_age(tmp_path: Path) -> None:
    resent = _gate(f"a-g-{LEAD}", LEAD)
    frames = [*_replayed([*_PRIOR_RESUME, *_parked_at_steer_gate()]), BOUNDARY, resent]
    assert await _client_output(tmp_path, frames) == CODER_RECAP + "\n"


# AC5(e)
@pytest.mark.anyio
@pytest.mark.usefixtures("frozen_now")
async def test_quiet_attach_ctrl_c_after_a_resume_shows_no_age(tmp_path: Path) -> None:
    running = [*_PRIOR_RESUME, _task_lead(), _dispatch("d1", LEAD, "coder", "writing code"),
               _sub_work("d1")]
    frames = [*_replayed(running), BOUNDARY, *_ctrl_c("a", LEAD, "sub:d1")]
    assert await _client_output(tmp_path, frames) == CODER_RECAP + "\n"


# AC5(f)
@pytest.mark.anyio
@pytest.mark.usefixtures("frozen_now")
async def test_quiet_attach_other_clients_leave_after_a_resume_shows_no_age(
    tmp_path: Path,
) -> None:
    running = [*_PRIOR_RESUME, ev("ip", "workflow_resumed", in_process=True),
               _task_lead(resumed=True), _dispatch("d1", LEAD, "coder", "writing code"),
               _sub_work("d1")]
    frames = [*_replayed(running), BOUNDARY, *_ctrl_c("a", LEAD, "sub:d1", source="ctrl-d")]
    assert await _client_output(tmp_path, frames) == CODER_RECAP + "\n"


# AC7(a)-(c)
@pytest.mark.usefixtures("frozen_now")
@pytest.mark.parametrize(("splash", "top"), [
    pytest.param(True, ["banner", ""], id="splash"),
    pytest.param(False, ["feat · demo · resuming", ""], id="header"),
])
@pytest.mark.parametrize(("texts", "block"), [
    pytest.param((PROSE,), [BLOCK, ""], id="prose"),
    pytest.param((), [], id="no-prose"),
])
def test_message_block_sits_one_blank_above_the_recap(
    splash: bool, top: list[str], texts: tuple[str, ...], block: list[str],
) -> None:
    events = _timed(_saying(_spec_review(), SPEC_REVIEW, *texts))
    assert (_prompt_screen(events, [LIVE_GATE], splash=splash)
            == [*top, *block, SPEC_RECAP_AGED, "", "> PROMPT"])


# AC7(d)
def test_parked_steer_gate_attach_prints_the_message_above_the_recap() -> None:
    lines = _prompt_screen(_saying(_parked_at_steer_gate(), LEAD, PROSE),
                           [_gate(f"a-g-{LEAD}", LEAD)], splash=False)
    assert lines == ["feat · demo · resuming", "", BLOCK, "", CODER_RECAP, "", "> PROMPT"]


# AC7(e): no anchor (untimed), no label, nothing cut off.
def test_message_alone_without_a_recap_line_prints_nothing() -> None:
    events = _saying(_opening_thinking(), THRESHOLD, PROSE)
    assert _prompt_screen(events, [LIVE_GATE]) == ["banner", "", "> PROMPT"]


_RUNNING_CODER = [_task_lead(), _dispatch("d1", LEAD, "coder", "writing code"), _sub_work("d1")]


@pytest.mark.anyio
@pytest.mark.parametrize("frames", [
    pytest.param(_resumed_at(_saying(_cut_off_then_resume("coder", "writing code"), LEAD, PROSE)),
                 id="resumed-hold-gate"),
    pytest.param([*_replayed(_saying(_parked_at_steer_gate(), LEAD, PROSE)), BOUNDARY,
                  _gate(f"a-g-{LEAD}", LEAD)], id="parked-steer-gate-attach"),
    pytest.param([*_replayed(_saying(_RUNNING_CODER, LEAD, PROSE)), BOUNDARY,
                  *_ctrl_c("a", LEAD, "sub:d1")], id="quiet-attach-ctrl-c"),
])
async def test_every_recap_path_prints_the_message_block(
    tmp_path: Path, frames: list[Event],
) -> None:
    assert await _client_output(tmp_path, frames) == f"{BLOCK}\n\n{CODER_RECAP}\n"


# AC8
@pytest.mark.anyio
async def test_message_block_is_scrollback_styled_with_color_on(tmp_path: Path) -> None:
    text = "Checking **criterion 4**.\x07"
    frames = _resumed_at(_saying(_cut_off_then_resume("coder", "writing code"), LEAD, text))
    out = await _client_output(tmp_path, frames, color=True)
    block = render_inert_question(text, width=80, color=True)
    assert out == f"{block}\n\n\x1b[2m{CODER_RECAP}\x1b[0m\n"
    assert out.startswith("\x1b[1m• \x1b[0m")
    assert "**" not in out
    assert "^G" in out and "\x07" not in out


def test_message_block_wraps_at_the_driver_width() -> None:
    prose = "Checking criterion four against the recorded transcript before moving on to five."
    lines = _prompt_screen(_saying(_parked_at_steer_gate(), LEAD, prose),
                           [_gate(f"a-g-{LEAD}", LEAD)], splash=False, width=40)
    assert lines == [
        "feat · demo · resuming", "",
        "• Checking criterion four against the",
        "  recorded transcript before moving on",
        "  to five.", "",
        CODER_RECAP, "", "> PROMPT",
    ]


def test_multi_paragraph_message_keeps_its_inner_blank_row() -> None:
    lines = _prompt_screen(
        _saying(_parked_at_steer_gate(), LEAD, "First paragraph.\n\nSecond paragraph."),
        [_gate(f"a-g-{LEAD}", LEAD)], splash=False)
    assert lines == [
        "feat · demo · resuming", "",
        "• First paragraph.", "",
        "  Second paragraph.", "",
        CODER_RECAP, "", "> PROMPT",
    ]

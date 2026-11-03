"""Resume recap: the dim `was:` line printed at the first qualifying interrupt gate."""
from __future__ import annotations

import io

from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from tests._event_fixtures import _TTY, agent_text, ev
from tests.test_narrative_interrupt_recap import (
    LEAD,
    LIVE_GATE,
    Event,
    _ctrl_c,
    _cut_off_then_resume,
    _dispatch,
    _opening_thinking,
    _sub_work,
    _task_lead,
)
from tests.test_resume_startup_spinner import Screen, _trimmed

BOUNDARY: Event = {"type": "replay_complete"}
CODER_RECAP = "was: task 1/2 · writing code"


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
    assert out.getvalue() == CODER_RECAP + "\n"
    drv.on_event(agent_text("lt", "Live prose after the recap.", agent_id=LEAD))
    assert out.getvalue() == CODER_RECAP + "\n\n• Live prose after the recap.\n"


def test_recap_before_the_replay_boundary_prints_nothing() -> None:
    out = _Pipe()
    drv = _resume_driver(out)
    for e in _replayed(_cut_off_then_resume("coder", "writing code")):
        drv.on_event(e)
    drv.print_interrupt_recap()
    assert out.getvalue() == ""


def test_recap_is_dropped_not_deferred_when_a_prompt_is_already_up() -> None:
    out = _Pipe()
    drv = _resume_driver(out)
    for e in [*_replayed(_cut_off_then_resume("coder", "writing code")), BOUNDARY]:
        drv.on_event(e)
    drv.prepare_for_prompt()
    drv.print_interrupt_recap()
    drv.flush_deferred()
    drv.print_interrupt_recap()             # once per attach, even if the first was dropped
    assert "was:" not in out.getvalue()


def _prompt_screen(replayed: list[Event], live: list[Event], *, splash: bool = True,
                   recap: bool = True) -> list[str]:
    """Opening, replay, live events, recap, then the solicit's suspend + prepare + caret."""
    out = _TTY()
    out.write("banner\n\n" if splash else "feat · demo · resuming\n")
    status = LiveStatus(out)
    status._width_fn = lambda: 80
    drv = LiveDriver(stream=out, color=False, width=80, status=status,
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
    assert lines == ["banner", "", "", "> PROMPT"]
    assert _prompt_screen(events, [LIVE_GATE], recap=True) == lines

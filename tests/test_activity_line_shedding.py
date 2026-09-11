"""The activity line's droppable hint tail: a narrow row sheds keypress hints, never the
retry-backoff countdown. LiveStatus._draw clips every footer row to width - 1, and
clip_display reserves a column for its ellipsis while scanning, so a row survives that
clip byte-identical only at width - 2 display columns or less."""

from __future__ import annotations

import pytest

from imp.cli import narrative as nv
from imp.cli import palette as pal
from imp.cli.live_driver import compose_activity_line

# The Goal's reference activity: a 660s backoff with nothing elapsed renders an `11m left`
# tail, so the three ladder candidates measure 88 / 59 / 36 display columns.
_START = "2026-01-01T00:00:00+00:00"
_R = "press r to retry now"
_P = "press p to switch provider"


def _waiting(total: float = 660.0) -> nv.Activity:
    return nv.Activity(verb="waiting for limit reset", countdown_total=total,
                       start_ts=_START)


def _line(width: int, *, color: bool = False, total: float = 660.0) -> str:
    return compose_activity_line(activity=_waiting(total), frame=0, color=color,
                                 now_iso=_START, width=width)


def test_width_is_a_required_keyword() -> None:
    with pytest.raises(TypeError, match="width"):
        compose_activity_line(activity=_waiting(), frame=0, color=False, now_iso=_START)


def test_at_eighty_columns_the_countdown_survives_and_the_p_hint_sheds() -> None:
    line = _line(80)
    assert line.endswith("11m left")
    assert _R in line
    assert _P not in line
    assert pal.clip_display(line, 79) == line

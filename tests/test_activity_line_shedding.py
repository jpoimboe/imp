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


def test_a_wide_row_keeps_both_hints_in_order_before_the_countdown() -> None:
    assert _line(120).endswith("waiting for limit reset · press r to retry now"
                               " · press p to switch provider · 11m left")


def test_at_fifty_columns_both_hints_shed_and_the_countdown_stays() -> None:
    line = _line(50)
    assert _R not in line
    assert _P not in line
    assert line.endswith("11m left")
    assert pal.clip_display(line, 49) == line


def test_the_p_hint_sheds_between_ninety_and_eighty_nine_columns() -> None:
    wide = _line(90)                       # the 88-column candidate survives at 90
    assert _P in wide
    assert pal.clip_display(wide, 89) == wide
    narrow = _line(89)                     # a width - 1 budget would keep it here
    assert _R in narrow
    assert _P not in narrow
    assert pal.clip_display(narrow, 88) == narrow


def test_the_r_hint_sheds_between_sixty_one_and_sixty_columns() -> None:
    assert _R in _line(61)                 # the 59-column candidate survives at 61
    bare = _line(60)
    assert _R not in bare
    assert _P not in bare
    assert bare.endswith("11m left")
    assert pal.clip_display(bare, 59) == bare


def test_a_coloured_line_is_measured_in_display_columns_not_bytes() -> None:
    line = _line(90, color=True)
    assert _P in line                      # a len-based budget sheds both hints here
    assert pal.display_width(line) == 88
    assert len(line) > 88                  # SGR bytes that len would have counted


def test_when_nothing_fits_the_narrowest_candidate_is_returned_whole() -> None:
    line = _line(20)                       # below the 36-column narrowest candidate
    assert line == "⠋ waiting for limit reset · 11m left"
    assert "…" not in line                 # only _draw's clip may shorten it


def test_a_sub_minute_countdown_shows_no_hints_at_any_width() -> None:
    for width in (200, 80, 20):
        line = _line(width, total=30.0)
        assert _R not in line
        assert _P not in line
        assert line.endswith("30s left")


def test_the_pause_hint_renders_unchanged_on_a_wide_row() -> None:
    act = nv.Activity(verb="waiting for tuhe55, koze82 to merge", dep_wait=True,
                      start_ts=_START)
    line = compose_activity_line(activity=act, frame=0, color=False,
                                 now_iso="2026-01-01T00:04:00+00:00", width=200)
    assert line.endswith("waiting for tuhe55, koze82 to merge"
                         " · press ctrl-d to pause · 4m 0s")


def test_the_pause_hint_outlives_both_keypress_hints_when_the_row_narrows() -> None:
    """The falsifying case: at 80 columns a dep_wait countdown sheds both keypress hints
    and keeps the pause hint, so the pause hint cannot have joined the droppable tail."""
    act = nv.Activity(verb="waiting for limit reset", countdown_total=660.0,
                      dep_wait=True, start_ts=_START)
    line = compose_activity_line(activity=act, frame=0, color=False, now_iso=_START,
                                 width=80)
    assert line == "⠋ waiting for limit reset · press ctrl-d to pause · 11m left"
    assert pal.clip_display(line, 79) == line


def test_the_pending_provider_notice_renders_unchanged_on_a_wide_row() -> None:
    line = compose_activity_line(activity=nv.Activity(kind="agent", verb="thinking"),
                                 frame=0, color=False, now_iso=_START, width=200,
                                 pending_provider="codex")
    assert line.endswith("thinking · switching to codex after this step · 0s")


def test_the_pending_provider_notice_outlives_both_keypress_hints_when_narrow() -> None:
    """Same falsifying shape for the notice: it survives 80 columns while both keypress
    hints shed."""
    line = compose_activity_line(activity=_waiting(), frame=0, color=False,
                                 now_iso=_START, width=80, pending_provider="codex")
    assert line == ("⠋ waiting for limit reset"
                    " · switching to codex after this step · 11m left")
    assert pal.clip_display(line, 79) == line

# tests/test_live_status.py
from __future__ import annotations

import io

from imp.cli.live_status import LiveStatus


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def test_first_draw_writes_owned_blank_then_each_row_col_reset() -> None:
    out = _TTY()
    s = LiveStatus(out)
    s._width_fn = lambda: 80
    s.update(lambda f: ["activity", "", "position bar"])
    assert out.getvalue() == "\x1b[?25l\n\r\x1b[Kactivity\n\r\x1b[K\n\r\x1b[Kposition bar"


def test_in_place_rewrite_climbs_to_first_row_then_rewrites_each() -> None:
    out = _TTY()
    s = LiveStatus(out)
    s._width_fn = lambda: 80
    s.update(lambda f: ["a", "", "bar"])
    out.truncate(0)
    out.seek(0)
    s.update(lambda f: ["a2", "", "bar2"])
    v = out.getvalue()
    assert v.startswith("\r\x1b[2A")        # \r + up (rows-1) onto the first row
    assert v.count("\r\x1b[K") == 3         # each of the 3 rows rewritten col-reset
    assert v.endswith("bar2")


def test_hide_clears_every_row_and_lands_on_owned_blank() -> None:
    out = _TTY()
    s = LiveStatus(out)
    s._width_fn = lambda: 80
    s.update(lambda f: ["a", "", "bar"])
    out.truncate(0)
    out.seek(0)
    s.hide()
    assert out.getvalue().count("\x1b[K") == 3     # all three rows cleared
    assert out.getvalue().count("\x1b[1A") == 3    # climb past all rows onto the blank


def test_non_tty_writes_nothing() -> None:
    s = _Pipe()
    ls = LiveStatus(s)
    ls.update(lambda f: ["should not appear"])
    ls._frame += 1
    ls._draw()
    ls.hide()
    ls.reset()
    with ls.suspend():
        pass
    assert s.getvalue() == ""


def test_suspend_clears_then_yields() -> None:
    s = _TTY()
    ls = LiveStatus(s)
    ls.update(lambda f: ["busy"])
    with ls.suspend():
        # on enter the line is erased (plus cursor-up onto the status-owned blank)
        assert "\x1b[K" in s.getvalue() and "\x1b[1A" in s.getvalue()


def test_suspend_suppresses_concurrent_tick_draw() -> None:
    """On a real TTY the spinner tick task keeps firing _draw() while a prompt
    awaits input. suspend() must suppress those draws, or the spinner overwrites
    the user's input line. (Tests elsewhere use a non-TTY pipe where draws no-op,
    so this race is invisible to them — it must be asserted on a TTY.)"""
    s = _TTY()
    ls = LiveStatus(s)
    ls.update(lambda f: ["busy"])
    with ls.suspend():
        s.truncate(0)
        s.seek(0)                           # ignore the erase already written on enter
        ls._frame += 1                      # simulate a tick firing mid-prompt
        ls._draw()
        assert s.getvalue() == ""           # nothing drawn over the prompt
    # On exit, drawing resumes and the line is re-shown.
    assert s.getvalue().endswith("busy")


def test_hide_after_narrowing_climbs_reflowed_physical_lines() -> None:
    # The resize-repeat bug: a permanent print calls hide() on a footer whose rows
    # were committed at the OLD (wider) width. After the terminal narrowed those rows
    # soft-wrap, so erase must climb the reflowed PHYSICAL line count — one-per-logical
    # -row leaves the wrapped tails behind as a duplicated footer.
    out = _TTY()
    s = LiveStatus(out)
    w = [40]
    s._width_fn = lambda: w[0]
    # Two rows, 30 and 20 cols wide, drawn at width 40 (each one physical line).
    s.update(lambda f: ["x" * 30, "y" * 20])
    assert s._physical_rows() == 2                   # nothing wrapped yet
    w[0] = 10                                         # terminal shrinks to 10 cols
    # 30 cols → ceil(30/10)=3 lines; 20 cols → ceil(20/10)=2 lines → 5 physical lines.
    assert s._physical_rows() == 5
    out.truncate(0)
    out.seek(0)
    s.hide()
    v = out.getvalue()
    assert v.count("\x1b[K") == 5                     # every reflowed physical line cleared
    assert v.count("\x1b[1A") == 5                    # climb lands on the owned blank


def test_physical_rows_counts_empty_row_as_one_line() -> None:
    out = _TTY()
    s = LiveStatus(out)
    w = [8]
    s._width_fn = lambda: w[0]
    s.update(lambda f: ["activity", "", "bar"])    # 8-col row clipped to 7, blank, 3
    assert s._physical_rows() == 3                    # each fits: 1 + 1(blank) + 1
    w[0] = 4
    # "activi…" is 7 cols → ceil(7/4)=2; blank → 1; "bar" (3) → 1 → 4 physical lines.
    assert s._physical_rows() == 4


def test_double_hide_does_not_cursor_up() -> None:
    out = _TTY()
    s = LiveStatus(out)
    s._width_fn = lambda: 80
    s.update(lambda f: ["ready"])
    s.hide()
    out.truncate(0)
    out.seek(0)
    s.hide()                                         # already hidden → strict no-op
    assert out.getvalue() == ""


def test_status_clipped_to_width_minus_one() -> None:
    out = _TTY()
    s = LiveStatus(out)
    s._width_fn = lambda: 6
    s.update(lambda f: ["abcdefghij"])
    assert "abcd…" in out.getvalue()           # 5 cols = width-1


def test_narrowing_erases_reflowed_lines_then_redraws_in_place() -> None:
    # On narrowing the committed row soft-wraps; the footer fully erases the reflowed
    # physical lines (climbing onto the owned blank) and redraws IN PLACE, so no blank
    # gap is stranded between the narrative and the footer.
    out = _TTY()
    s = LiveStatus(out)
    w = [20]
    s._width_fn = lambda: w[0]
    s.update(lambda f: ["first frame here"])       # 16 cols at width 20
    out.truncate(0)
    out.seek(0)
    w[0] = 4
    s.update(lambda f: ["second"])                 # width 4 → ceil(16/4)=4 phys lines
    v = out.getvalue()
    assert v.count("\x1b[1A") == 4                    # climbed all four reflowed lines
    assert v.count("\x1b[K") == 4 + 1                 # 4 erased + 1 redrawn row


def test_narrowing_multirow_erases_all_reflowed_lines() -> None:
    out = _TTY()
    s = LiveStatus(out)
    w = [20]
    s._width_fn = lambda: w[0]
    s.update(lambda f: ["a row", "b row", "c row"])  # three 5-col rows at width 20
    out.truncate(0)
    out.seek(0)
    w[0] = 4
    s.update(lambda f: ["a row", "b row", "c row"])  # width 4 → each 5→2 phys = 6
    v = out.getvalue()
    assert v.count("\x1b[1A") == 6                       # all six reflowed lines climbed
    assert v.count("\x1b[K") == 6 + 3                    # 6 erased + 3 redrawn rows


def test_ack_on_fresh_status_draws_in_place_no_leading_newline() -> None:
    out = _TTY()
    s = LiveStatus(out)
    s._width_fn = lambda: 80
    s.show_stop_ack("stopping…")             # ack on a fresh (never-drawn) status
    assert out.getvalue() == "\x1b[?25l\r\x1b[K\x1b[2mstopping…\x1b[0m"
    assert s._rows == 1 and s._visible is True


def test_ack_over_multirow_block_draws_below_the_preserved_owned_blank() -> None:
    out = _TTY()
    s = LiveStatus(out)
    s._width_fn = lambda: 80
    s.update(lambda f: ["activity", "", "bar"])   # a 3-row footer is visible
    assert s._rows == 3
    out.truncate(0)
    out.seek(0)
    s.show_stop_ack("stopping…")
    v = out.getvalue()
    assert v.count("\x1b[1A") == 3           # _erase() climbed past ALL three rows
    assert v.endswith("\n\r\x1b[K\x1b[2mstopping…\x1b[0m")
    assert s._rows == 1 and s._visible is True


def test_ack_over_block_then_redraw_climbs_one_row_and_preserves_blank() -> None:
    out = _TTY()
    s = LiveStatus(out)
    s._width_fn = lambda: 80
    s.update(lambda f: ["activity", "", "bar"])
    s.show_stop_ack("stopping…")
    out.truncate(0)
    out.seek(0)
    s.update(lambda f: ["a2", "", "bar2"])        # block redraws over the ack
    v = out.getvalue()
    assert v.count("\x1b[1A") == 1
    assert v.startswith("\r\x1b[K\x1b[1A")
    assert "\n\r\x1b[Ka2" in v
    assert v.rstrip().endswith("bar2")


def test_stop_ack_on_skip_drawn_footer_draws_in_place_no_climb() -> None:
    out = _TTY()
    s = LiveStatus(out)
    s._width_fn = lambda: 80
    s.skip_leading_blank()
    s.update(lambda f: ["spinner row"])       # skip-drawn: owns no blank
    assert s._owns_lead_blank is False
    out.truncate(0)
    out.seek(0)
    s.show_stop_ack("stopped")
    v = out.getvalue()
    assert "\n" not in v                          # ack drawn in place, not pushed down a row
    assert "stopped" in v
    assert v.count("\x1b[1A") == 0                # no climb onto the committed blank


def test_stop_ack_on_owned_blank_footer_still_pushes_leading_newline() -> None:
    out = _TTY()
    s = LiveStatus(out)
    s._width_fn = lambda: 80
    s.update(lambda f: ["busy"])              # no skip -> owns its blank
    assert s._owns_lead_blank is True
    out.truncate(0)
    out.seek(0)
    s.show_stop_ack("stopped")
    v = out.getvalue()
    assert v.endswith("\n\r\x1b[K\x1b[2mstopped\x1b[0m")   # leading "\n" before the ack


def test_hide_cursor_writes_hide_once_and_is_idempotent() -> None:
    out = _TTY()
    s = LiveStatus(out)
    s._hide_cursor()
    s._hide_cursor()                          # already hidden → no second write
    assert out.getvalue() == "\x1b[?25l"
    assert s._cursor_hidden is True


def test_show_cursor_writes_show_once_and_is_idempotent() -> None:
    out = _TTY()
    s = LiveStatus(out)
    s._hide_cursor()
    out.truncate(0)
    out.seek(0)
    s._show_cursor()
    s._show_cursor()                          # already shown → no second write
    assert out.getvalue() == "\x1b[?25h"
    assert s._cursor_hidden is False


def test_show_cursor_is_noop_when_not_hidden() -> None:
    out = _TTY()
    s = LiveStatus(out)
    s._show_cursor()                          # never hidden → strict no-op
    assert out.getvalue() == ""
    assert s._cursor_hidden is False


def test_show_cursor_swallows_closed_stream_error() -> None:
    out = _TTY()
    s = LiveStatus(out)
    s._hide_cursor()          # cursor now hidden
    out.close()               # stream closed under it, as at interpreter shutdown
    s._show_cursor()          # must return without raising ValueError
    assert s._cursor_hidden is False   # flag cleared even though the write was swallowed


def test_non_tty_hide_cursor_writes_nothing() -> None:
    out = _Pipe()
    s = LiveStatus(out)
    s._hide_cursor()
    assert out.getvalue() == ""
    assert s._cursor_hidden is False


def test_atexit_registers_show_cursor_for_tty(monkeypatch) -> None:
    registered: list = []
    monkeypatch.setattr(
        "imp.cli.live_status.atexit.register", lambda fn: registered.append(fn)
    )
    s = LiveStatus(_TTY())
    assert s._show_cursor in registered


def test_atexit_not_registered_for_non_tty(monkeypatch) -> None:
    registered: list = []
    monkeypatch.setattr(
        "imp.cli.live_status.atexit.register", lambda fn: registered.append(fn)
    )
    LiveStatus(_Pipe())
    assert registered == []


def test_first_visible_draw_hides_cursor() -> None:
    out = _TTY()
    s = LiveStatus(out)
    s._width_fn = lambda: 80
    s.update(lambda f: ["busy"])
    assert "\x1b[?25l" in out.getvalue()
    assert s._cursor_hidden is True


def test_hide_cursor_emitted_once_across_frames() -> None:
    out = _TTY()
    s = LiveStatus(out)
    s._width_fn = lambda: 80
    s.update(lambda f: ["busy"])
    s._frame += 1
    s._draw()
    s._frame += 1
    s._draw()
    assert out.getvalue().count("\x1b[?25l") == 1     # no duplicate while already hidden


def test_erase_shows_cursor() -> None:
    out = _TTY()
    s = LiveStatus(out)
    s._width_fn = lambda: 80
    s.update(lambda f: ["busy"])
    out.truncate(0)
    out.seek(0)
    s.hide()                                   # hide() -> _erase()
    assert "\x1b[?25h" in out.getvalue()
    assert s._cursor_hidden is False


def test_reset_shows_cursor() -> None:
    out = _TTY()
    s = LiveStatus(out)
    s._width_fn = lambda: 80
    s.update(lambda f: ["busy"])
    out.truncate(0)
    out.seek(0)
    s.reset()                                  # reset() -> _erase()
    assert "\x1b[?25h" in out.getvalue()
    assert s._cursor_hidden is False


def test_suspend_entry_shows_cursor_and_exit_rehides() -> None:
    out = _TTY()
    s = LiveStatus(out)
    s._width_fn = lambda: 80
    s.update(lambda f: ["busy"])
    out.truncate(0)
    out.seek(0)
    with s.suspend():
        assert "\x1b[?25h" in out.getvalue()   # shown for the interactive prompt
        assert s._cursor_hidden is False
    assert s._cursor_hidden is True            # exit redraws -> re-hidden


def test_hide_then_erase_ends_cursor_shown() -> None:
    out = _TTY()
    s = LiveStatus(out)
    s._width_fn = lambda: 80
    s.update(lambda f: ["busy"])
    s.hide()
    assert s._cursor_hidden is False


def test_narrowing_redraw_ends_cursor_hidden() -> None:
    out = _TTY()
    s = LiveStatus(out)
    w = [20]
    s._width_fn = lambda: w[0]
    s.update(lambda f: ["first frame here"])     # drawn at width 20, cursor hidden
    out.truncate(0)
    out.seek(0)
    w[0] = 4
    s.update(lambda f: ["second"])               # width now 4 -> narrowing
    v = out.getvalue()
    assert "\x1b[?25h" in v and "\x1b[?25l" in v     # clear shows, frame re-hides
    assert v.rindex("\x1b[?25l") > v.index("\x1b[?25h")   # re-hide sequenced AFTER the show
    assert s._cursor_hidden is True


def test_non_tty_emits_no_cursor_escapes() -> None:
    out = _Pipe()
    s = LiveStatus(out)
    s.update(lambda f: ["x"])
    s._frame += 1
    s._draw()
    s.hide()
    s.reset()
    with s.suspend():
        pass
    v = out.getvalue()
    assert "\x1b[?25l" not in v and "\x1b[?25h" not in v


def test_skip_leading_blank_first_draw_omits_owned_blank() -> None:
    out = _TTY()
    s = LiveStatus(out)
    s._width_fn = lambda: 80
    s.skip_leading_blank()
    s.update(lambda f: ["spinner row"])
    # No leading "\n": the row is drawn directly on the committed blank below.
    assert out.getvalue() == "\x1b[?25l\r\x1b[Kspinner row"
    assert s._owns_lead_blank is False


def test_skip_drawn_footer_erase_stays_in_place_no_climb() -> None:
    out = _TTY()
    s = LiveStatus(out)
    s._width_fn = lambda: 80
    s.skip_leading_blank()
    s.update(lambda f: ["spinner row"])       # owns no blank
    out.truncate(0)
    out.seek(0)
    s.hide()                                      # -> _erase()
    v = out.getvalue()
    assert v.count("\x1b[K") == 1                 # its one row cleared
    assert v.count("\x1b[1A") == 0                # but NOT climbed onto the committed blank


def test_skip_lead_is_one_shot_next_draw_owns_blank_again() -> None:
    out = _TTY()
    s = LiveStatus(out)
    s._width_fn = lambda: 80
    s.skip_leading_blank()
    s.update(lambda f: ["one row"])            # skip consumed here
    out.truncate(0)
    out.seek(0)
    s.update(lambda f: ["a", "", "bar"])       # row-count change -> else path, owns blank
    v = out.getvalue()
    assert "\n\r\x1b[Ka" in v   # a leading owned blank was written
    assert s._owns_lead_blank is True


def test_default_footer_still_owns_blank_and_climbs() -> None:
    out = _TTY()
    s = LiveStatus(out)
    s._width_fn = lambda: 80
    s.update(lambda f: ["busy"])               # no skip -> owns its blank
    assert s._owns_lead_blank is True
    out.truncate(0)
    out.seek(0)
    s.hide()
    assert out.getvalue().count("\x1b[1A") == 1   # climbs onto the owned blank (unchanged)


def test_live_status_has_no_span_clock() -> None:
    s = LiveStatus(_TTY())
    assert not hasattr(s, "_session_start")
    assert not hasattr(s, "restart_span")
    assert not hasattr(s, "elapsed")


def test_tick_advances_frame_only() -> None:
    out = _TTY()
    s = LiveStatus(out)
    s._width_fn = lambda: 80
    s.update(lambda frame: [f"f{frame}"])
    s._frame += 1
    s._draw()
    assert out.getvalue().endswith("\r\x1b[Kf1")   # spinner frame still advances (AC8)

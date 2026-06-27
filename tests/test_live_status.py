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


def _clock():
    t = {"v": 0.0}
    def now() -> float:
        return t["v"]
    return t, now


def test_first_draw_writes_owned_blank_then_each_row_col_reset() -> None:
    out = _TTY()
    s = LiveStatus(out, now=lambda: 0.0)
    s._width_fn = lambda: 80
    s.update(lambda f, e: ["activity", "", "position bar"])
    assert out.getvalue() == "\n\r\x1b[Kactivity\n\r\x1b[K\n\r\x1b[Kposition bar"


def test_in_place_rewrite_climbs_to_first_row_then_rewrites_each() -> None:
    out = _TTY()
    s = LiveStatus(out, now=lambda: 0.0)
    s._width_fn = lambda: 80
    s.update(lambda f, e: ["a", "", "bar"])
    out.truncate(0)
    out.seek(0)
    s.update(lambda f, e: ["a2", "", "bar2"])
    v = out.getvalue()
    assert v.startswith("\r\x1b[2A")        # \r + up (rows-1) onto the first row
    assert v.count("\r\x1b[K") == 3         # each of the 3 rows rewritten col-reset
    assert v.endswith("bar2")


def test_hide_clears_every_row_and_lands_on_owned_blank() -> None:
    out = _TTY()
    s = LiveStatus(out, now=lambda: 0.0)
    s._width_fn = lambda: 80
    s.update(lambda f, e: ["a", "", "bar"])
    out.truncate(0)
    out.seek(0)
    s.hide()
    assert out.getvalue().count("\x1b[K") == 3     # all three rows cleared
    assert out.getvalue().count("\x1b[1A") == 3    # climb past all rows onto the blank


def test_tick_advances_frame_and_elapsed() -> None:
    t, now = _clock()
    s = _TTY()
    ls = LiveStatus(s, now=now)
    ls.update(lambda frame, elapsed: [f"f{frame}/{int(elapsed)}"])
    t["v"] = 3.0
    ls._frame += 1            # simulate one tick (tick() loops over this)
    ls._draw()
    assert s.getvalue().endswith("\r\x1b[Kf1/3")


def test_hide_preserves_session_timer_reset_does_not() -> None:
    t, now = _clock()
    s = _TTY()
    ls = LiveStatus(s, now=now)
    ls.update(lambda f, e: [f"e{int(e)}"])
    t["v"] = 5.0
    ls.hide()                 # erase but keep the session start
    ls.update(lambda f, e: [f"e{int(e)}"])
    assert s.getvalue().endswith("e5")     # elapsed continued across hide
    ls.reset()                # ends the session
    ls.update(lambda f, e: [f"e{int(e)}"])
    assert s.getvalue().endswith("e0")     # timer restarted after reset


def test_suspend_resets_session_timer_on_exit() -> None:
    """The suspended interval is the interactive prompt — the human owns the
    terminal, the agent is NOT working. A wait_for_user reply returns as a
    tool_result (no user_message event, so the driver's restart_span never fires
    for it); the span must reset on suspend exit instead. Otherwise the resumed
    'thinking' line counts the human's read/type time — the `imp new` first-turn
    "thinking 26s instead of 0s" bug."""
    t, now = _clock()
    s = _TTY()
    ls = LiveStatus(s, now=now)
    ls.update(lambda f, e: [f"e{int(e)}"])     # session starts at t=0
    t["v"] = 26.0                              # human spends 26s typing at the prompt
    with ls.suspend():
        pass                                   # ... reply submitted, prompt closes
    ls.update(lambda f, e: [f"e{int(e)}"])
    assert s.getvalue().endswith("e0")         # resumed elapsed counts from the reply


def test_suspend_does_not_start_an_unstarted_timer() -> None:
    """When no span is running (session timer never started), suspend exit must
    leave it unstarted — update() owns first-start, not suspend()."""
    t, now = _clock()
    s = _TTY()
    ls = LiveStatus(s, now=now)
    t["v"] = 9.0
    with ls.suspend():
        pass
    assert ls._session_start is None


def test_non_tty_writes_nothing() -> None:
    _t, now = _clock()
    s = _Pipe()
    ls = LiveStatus(s, now=now)
    ls.update(lambda f, e: ["should not appear"])
    ls._frame += 1
    ls._draw()
    ls.hide()
    ls.reset()
    with ls.suspend():
        pass
    assert s.getvalue() == ""


def test_suspend_clears_then_yields() -> None:
    _t, now = _clock()
    s = _TTY()
    ls = LiveStatus(s, now=now)
    ls.update(lambda f, e: ["busy"])
    with ls.suspend():
        # on enter the line is erased (plus cursor-up onto the status-owned blank)
        assert "\x1b[K" in s.getvalue() and "\x1b[1A" in s.getvalue()


def test_suspend_suppresses_concurrent_tick_draw() -> None:
    """On a real TTY the spinner tick task keeps firing _draw() while a prompt
    awaits input. suspend() must suppress those draws, or the spinner overwrites
    the user's input line. (Tests elsewhere use a non-TTY pipe where draws no-op,
    so this race is invisible to them — it must be asserted on a TTY.)"""
    _t, now = _clock()
    s = _TTY()
    ls = LiveStatus(s, now=now)
    ls.update(lambda f, e: ["busy"])
    with ls.suspend():
        s.truncate(0)
        s.seek(0)                           # ignore the erase already written on enter
        ls._frame += 1                      # simulate a tick firing mid-prompt
        ls._draw()
        assert s.getvalue() == ""           # nothing drawn over the prompt
    # On exit, drawing resumes and the line is re-shown.
    assert s.getvalue().endswith("busy")


def test_restart_span_resets_elapsed() -> None:
    clock = {"t": 100.0}
    s = _TTY()
    ls = LiveStatus(s, now=lambda: clock["t"])
    seen: list[float] = []
    ls.update(lambda frame, elapsed: [seen.append(elapsed) or "x"])
    clock["t"] = 130.0
    ls.restart_span()           # new t0 = 130.0
    clock["t"] = 135.0          # advance 5s past the restart point
    ls.update(lambda frame, elapsed: [seen.append(elapsed) or "x"])
    assert seen[-1] == 5.0      # elapsed is from the new t0 (not 35.0 from the original)


def test_double_hide_does_not_cursor_up() -> None:
    out = _TTY()
    s = LiveStatus(out, now=lambda: 0.0)
    s._width_fn = lambda: 80
    s.update(lambda f, e: ["ready"])
    s.hide()
    out.truncate(0)
    out.seek(0)
    s.hide()                                         # already hidden → strict no-op
    assert out.getvalue() == ""


def test_status_clipped_to_width_minus_one() -> None:
    out = _TTY()
    s = LiveStatus(out, now=lambda: 0.0)
    s._width_fn = lambda: 6
    s.update(lambda f, e: ["abcdefghij"])
    assert "abcd…" in out.getvalue()           # 5 cols = width-1


def test_narrowing_from_visible_abandons_without_cursor_up() -> None:
    out = _TTY()
    s = LiveStatus(out, now=lambda: 0.0)
    widths = iter([20, 4])
    s._width_fn = lambda: next(widths)
    s.update(lambda f, e: ["first frame here"])       # drawn at width 20
    out.truncate(0)
    out.seek(0)
    s.update(lambda f, e: ["second"])                 # width now 4 → narrowing
    assert "\x1b[1A" not in out.getvalue()            # no cursor-up on narrowing
    assert "\n" in out.getvalue()                     # fresh redraw: new blank + status below


def test_narrowing_from_visible_multirow_abandons_in_place() -> None:
    out = _TTY()
    s = LiveStatus(out, now=lambda: 0.0)
    widths = iter([20, 4])
    s._width_fn = lambda: next(widths)
    s.update(lambda f, e: ["a row", "b row", "c row"])  # drawn at width 20, 3-row block
    out.truncate(0)
    out.seek(0)
    s.update(lambda f, e: ["a row", "b row", "c row"])  # width now 4 → narrowing
    v = out.getvalue()
    assert "\x1b[1A" not in v                           # no owned-blank ascent during abandon
    assert "\x1b[2A" in v                               # _clear_rows_in_place climbs to first row
    assert v.count("\x1b[K") == 3 + 3                   # 3 in-place clears + 3 fresh redraws
    assert "\n" in v                                    # fresh redraw: new blank + status below


def test_elapsed_zero_before_session_starts_and_tracks_after() -> None:
    clock = {"t": 100.0}
    ls = LiveStatus(_TTY(), now=lambda: clock["t"])
    assert ls.elapsed() == 0.0                      # no session yet
    ls.update(lambda f, e: ["x"])                   # session starts at 100.0
    clock["t"] = 104.0
    assert ls.elapsed() == 4.0


def test_ack_on_fresh_status_draws_in_place_no_leading_newline() -> None:
    out = _TTY()
    s = LiveStatus(out, now=lambda: 0.0)
    s._width_fn = lambda: 80
    s.show_stop_ack("stopping…")             # ack on a fresh (never-drawn) status
    assert out.getvalue() == "\r\x1b[K\x1b[2mstopping…\x1b[0m"
    assert s._rows == 1 and s._visible is True


def test_ack_over_multirow_block_draws_below_the_preserved_owned_blank() -> None:
    out = _TTY()
    s = LiveStatus(out, now=lambda: 0.0)
    s._width_fn = lambda: 80
    s.update(lambda f, e: ["activity", "", "bar"])   # a 3-row footer is visible
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
    s = LiveStatus(out, now=lambda: 0.0)
    s._width_fn = lambda: 80
    s.update(lambda f, e: ["activity", "", "bar"])
    s.show_stop_ack("stopping…")
    out.truncate(0)
    out.seek(0)
    s.update(lambda f, e: ["a2", "", "bar2"])        # block redraws over the ack
    v = out.getvalue()
    assert v.count("\x1b[1A") == 1
    assert v.startswith("\r\x1b[K\x1b[1A")
    assert "\n\r\x1b[Ka2" in v
    assert v.rstrip().endswith("bar2")

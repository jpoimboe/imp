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


def test_update_writes_carriage_return_and_erase_on_tty() -> None:
    _t, now = _clock()
    s = _TTY()
    ls = LiveStatus(s, now=now)
    ls.update(lambda frame, elapsed: f"frame={frame} elapsed={int(elapsed)}")
    assert s.getvalue() == "\nframe=0 elapsed=0"


def test_tick_advances_frame_and_elapsed() -> None:
    t, now = _clock()
    s = _TTY()
    ls = LiveStatus(s, now=now)
    ls.update(lambda frame, elapsed: f"f{frame}/{int(elapsed)}")
    t["v"] = 3.0
    ls._frame += 1            # simulate one tick (tick() loops over this)
    ls._draw()
    assert s.getvalue().endswith("\r\x1b[Kf1/3")


def test_hide_preserves_session_timer_reset_does_not() -> None:
    t, now = _clock()
    s = _TTY()
    ls = LiveStatus(s, now=now)
    ls.update(lambda f, e: f"e{int(e)}")
    t["v"] = 5.0
    ls.hide()                 # erase but keep the session start
    ls.update(lambda f, e: f"e{int(e)}")
    assert s.getvalue().endswith("e5")     # elapsed continued across hide
    ls.reset()                # ends the session
    ls.update(lambda f, e: f"e{int(e)}")
    assert s.getvalue().endswith("e0")     # timer restarted after reset


def test_non_tty_writes_nothing() -> None:
    _t, now = _clock()
    s = _Pipe()
    ls = LiveStatus(s, now=now)
    ls.update(lambda f, e: "should not appear")
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
    ls.update(lambda f, e: "busy")
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
    ls.update(lambda f, e: "busy")
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
    ls.update(lambda frame, elapsed: seen.append(elapsed) or "x")
    clock["t"] = 130.0
    ls.restart_span()           # new t0 = 130.0
    clock["t"] = 135.0          # advance 5s past the restart point
    ls.update(lambda frame, elapsed: seen.append(elapsed) or "x")
    assert seen[-1] == 5.0      # elapsed is from the new t0 (not 35.0 from the original)


def test_first_draw_after_hidden_writes_newline_then_status() -> None:
    out = _TTY()
    s = LiveStatus(out, now=lambda: 0.0)
    s._width_fn = lambda: 80
    s.update(lambda f, e: "ready")
    assert out.getvalue().startswith("\n")          # status-owned blank above
    assert "ready" in out.getvalue()


def test_hide_from_visible_erases_then_cursor_up() -> None:
    out = _TTY()
    s = LiveStatus(out, now=lambda: 0.0)
    s._width_fn = lambda: 80
    s.update(lambda f, e: "ready")
    out.truncate(0)
    out.seek(0)
    s.hide()
    assert "\x1b[K" in out.getvalue()               # erase the status row
    assert "\x1b[1A" in out.getvalue()              # then climb onto the blank


def test_double_hide_does_not_cursor_up() -> None:
    out = _TTY()
    s = LiveStatus(out, now=lambda: 0.0)
    s._width_fn = lambda: 80
    s.update(lambda f, e: "ready")
    s.hide()
    out.truncate(0)
    out.seek(0)
    s.hide()                                         # already hidden → strict no-op
    assert out.getvalue() == ""


def test_status_clipped_to_width_minus_one() -> None:
    out = _TTY()
    s = LiveStatus(out, now=lambda: 0.0)
    s._width_fn = lambda: 6
    s.update(lambda f, e: "abcdefghij")
    assert "abcd…" in out.getvalue()           # 5 cols = width-1


def test_narrowing_from_visible_abandons_without_cursor_up() -> None:
    out = _TTY()
    s = LiveStatus(out, now=lambda: 0.0)
    widths = iter([20, 4])
    s._width_fn = lambda: next(widths)
    s.update(lambda f, e: "first frame here")       # drawn at width 20
    out.truncate(0)
    out.seek(0)
    s.update(lambda f, e: "second")                 # width now 4 → narrowing
    assert "\x1b[1A" not in out.getvalue()          # no cursor-up on narrowing
    assert "\n" in out.getvalue()                   # fresh redraw: new blank + status below

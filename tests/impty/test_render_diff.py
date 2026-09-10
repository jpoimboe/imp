from __future__ import annotations

import contextlib

import pyte

from impty import view
from impty.driver import ImptyDriver, _diff_body
from impty.screen import TerminalScreen


def _rows(n: int) -> list[str]:
    return [f"r{i}" for i in range(n)]


def test_a_single_replaced_row_is_labelled_by_its_index() -> None:
    prev = _rows(10)
    cur = list(prev)
    cur[7] = "changed"
    assert _diff_body(prev, cur) == "[row 7]\nchanged"


def test_a_replaced_run_is_labelled_with_an_inclusive_range() -> None:
    prev = _rows(24)
    cur = list(prev)
    cur[20:23] = ["x20", "x21", "x22"]
    assert _diff_body(prev, cur) == "[rows 20-22]\nx20\nx21\nx22"


def test_a_replace_consuming_more_rows_than_it_emits_names_the_count() -> None:
    prev = _rows(10)
    cur = [*prev[:7], "merged", *prev[10:]]
    assert _diff_body(prev, cur) == "[row 7 replacing 3 rows]\nmerged"


def test_a_replace_emitting_more_rows_than_it_consumes_names_the_count() -> None:
    prev = _rows(24)
    cur = [*prev[:20], "a", "b", "c", *prev[21:]]
    assert _diff_body(prev, cur) == "[rows 20-22 replacing 1 row]\na\nb\nc"


def test_one_inserted_row_is_marked_inserted() -> None:
    prev = _rows(10)
    cur = [*prev[:7], "new", *prev[7:]]
    assert _diff_body(prev, cur) == "[row 7 inserted]\nnew"


def test_an_inserted_run_is_marked_inserted() -> None:
    prev = _rows(24)
    cur = [*prev[:21], "n1", "n2", "n3", *prev[21:]]
    assert _diff_body(prev, cur) == "[rows 21-23 inserted]\nn1\nn2\nn3"


def test_one_deleted_row_emits_no_rows() -> None:
    prev = _rows(24)
    cur = prev[:13] + prev[14:]
    assert _diff_body(prev, cur) == "[1 row deleted at row 13]"


def test_deleted_rows_are_counted_and_emit_no_rows() -> None:
    prev = _rows(24)
    assert _diff_body(prev, prev[3:]) == "[3 rows deleted at row 0]"


def test_identical_rows_are_the_no_text_change_marker() -> None:
    prev = _rows(24)
    assert _diff_body(prev, list(prev)) == "[no text change]"


def test_a_three_line_scroll_names_three_rows_not_twenty_three() -> None:
    # The whole point of a sequence diff: an index-wise diff reports 23 of 24
    # rows changed here, which costs more than sending the whole screen.
    prev = _rows(24)
    cur = [*prev[3:], "new1", "new2", "new3"]
    assert _diff_body(prev, cur) == (
        "[3 rows deleted at row 0]\n"
        "[rows 21-23 inserted]\nnew1\nnew2\nnew3"
    )


class _FakeSession:
    """Everything a render reads, over a real screen and no pty — so the diff
    tests are byte-driven and cannot flake on a settle."""

    def __init__(self, cols: int = 40, rows: int = 8) -> None:
        self.screen = TerminalScreen(cols, rows, history=200, ratio=0.5)
        self._stream = pyte.ByteStream(self.screen)
        self.returncode = None

    @property
    def rows(self):
        return self.screen.lines

    def feed(self, data: bytes) -> None:
        self._stream.feed(data)

    def send(self, data: str) -> None:
        """The imaginary child ignores keystrokes; tests paint via feed()."""

    def send_signal(self, sig: int) -> None:
        """No child to signal; tests paint the reaction via feed()."""

    def resize(self, rows: int, cols: int) -> None:
        self.screen.resize(lines=rows, columns=cols)

    def wait_idle(self, **kwargs) -> bool:
        """Nothing to settle: the screen is already whatever feed() painted."""
        return True

    def is_alive(self) -> bool:
        return self.returncode is None

    def close(self) -> None:
        self.returncode = 0

    def paused(self):
        return contextlib.nullcontext()

    def cursor(self):
        return view.cursor_pos(self.screen)

    def regime(self):
        return view.regime(self.screen)

    def lines(self, limit=None):
        return view.lines(self.screen, limit)


def _driver_on_a_fake_screen(cols: int = 40, rows: int = 8):
    d = ImptyDriver()
    s = _FakeSession(cols, rows)
    d._session = s
    return d, s


def _body(rendered: str) -> str:
    """Everything after the one-line status header."""
    return rendered.partition("\n")[2]


def _screenful(*rows: str, height: int = 8) -> str:
    """The rows a full-screen body carries: the given rows then blank ones."""
    return "\n".join([*rows, *[""] * (height - len(rows))])


def test_the_first_render_with_no_baseline_is_a_marked_full_screen() -> None:
    d, s = _driver_on_a_fake_screen()
    s.feed(b"alpha\r\nbravo\r\ncharlie\r\n")
    assert _body(d._render_diff(s)) == (
        "[full screen]\n" + _screenful("alpha", "bravo", "charlie"))


def test_the_status_header_survives_on_a_no_change_render() -> None:
    d, s = _driver_on_a_fake_screen()
    s.feed(b"alpha\r\nbravo\r\ncharlie\r\n")
    d._render_diff(s)
    rendered = d._render_diff(s)
    assert rendered.splitlines()[0] == "[alive=True regime=scroll cursor=(0,3)]"
    assert _body(rendered) == "[no text change]"


def test_an_unchanged_short_blank_screen_is_no_text_change() -> None:
    d, s = _driver_on_a_fake_screen(rows=8)
    d._render_diff(s)
    rendered = d._render_diff(s)
    assert rendered.splitlines()[0] == "[alive=True regime=scroll cursor=(0,0)]"
    assert _body(rendered) == "[no text change]"


def test_a_style_only_repaint_is_still_no_text_change() -> None:
    d, s = _driver_on_a_fake_screen()
    s.feed(b"alpha\r\nbravo\r\ncharlie\r\n")
    d._render_diff(s)
    s.feed(b"\x1b[H\x1b[7malpha\x1b[0m")   # same text, reverse video
    assert s.screen.buffer[0][0].reverse   # the cells really did change
    assert _body(d._render_diff(s)) == "[no text change]"


def test_a_scroll_of_three_lines_reports_three_rows_and_the_top_deletion() -> None:
    d, s = _driver_on_a_fake_screen(cols=80, rows=24)
    s.feed(b"\r\n".join(b"output line %d" % i for i in range(24)))
    d.view()
    s.feed(b"\r\nnew line A\r\nnew line B\r\nnew line C")
    assert _body(d._render_diff(s)) == (
        "[3 rows deleted at row 0]\n"
        "[rows 21-23 inserted]\nnew line A\nnew line B\nnew line C"
    )


def test_a_full_repaint_falls_back_to_the_marked_full_screen() -> None:
    d, s = _driver_on_a_fake_screen()
    s.feed(b"\r\n".join(b"line %d of the screen" % i for i in range(8)))
    d.view()
    s.feed(b"\x1b[2J\x1b[H" + b"\r\n".join(b"repainted row %d" % i for i in range(8)))
    assert _body(d._render_diff(s)) == (
        "[full screen]\n" + "\n".join(f"repainted row {i}" for i in range(8)))


def test_an_alt_screen_flip_resolves_through_the_length_rule() -> None:
    d, s = _driver_on_a_fake_screen()
    s.feed(b"\r\n".join(b"scrollback line %d" % i for i in range(8)))
    d.view()
    s.feed(b"\x1b[?1049h\x1b[2J\x1b[H"
           + b"\r\n".join(b"ALT pane row %d" % i for i in range(8)))
    rendered = d._render_diff(s)
    assert rendered.splitlines()[0].endswith("regime=screen cursor=(14,7)]")
    assert _body(rendered) == (
        "[full screen]\n" + "\n".join(f"ALT pane row {i}" for i in range(8)))


def test_view_never_diffs_and_still_updates_the_baseline() -> None:
    d, s = _driver_on_a_fake_screen()
    s.feed(b"alpha\r\nbravo\r\n")
    d._render_diff(s)
    s.feed(b"charlie\r\n")
    assert _body(d.view()) == _screenful("alpha", "bravo", "charlie")
    assert _body(d._render_diff(s)) == "[no text change]"


def test_view_full_returns_the_strip_and_baselines_the_screen_rows() -> None:
    d, s = _driver_on_a_fake_screen(cols=40, rows=4)
    s.feed(b"\r\n".join(b"row %d" % i for i in range(9)))
    assert _body(d.view(full=True)) == "\n".join(f"row {i}" for i in range(9))
    assert _body(d._render_diff(s)) == "[no text change]"


def test_a_send_that_renders_nothing_leaves_the_baseline_where_it_was() -> None:
    d, s = _driver_on_a_fake_screen()
    s.feed(b"\r\n".join(b"line %d of the screen" % i for i in range(8)))
    d.view()
    s.feed(b"\x1b[3;1H\x1b[2Ksecond line replaced")
    assert d.send("x", wait=False) == "[sent]"
    s.feed(b"\x1b[6;1H\x1b[2Kfifth line replaced")
    assert _body(d._render_diff(s)) == (
        "[row 2]\nsecond line replaced\n[row 5]\nfifth line replaced")


def test_a_resize_that_renders_nothing_leaves_the_baseline_where_it_was() -> None:
    d, s = _driver_on_a_fake_screen()
    s.feed(b"\r\n".join(b"line %d of the screen" % i for i in range(8)))
    d.view()
    s.feed(b"\x1b[3;1H\x1b[2Ksecond line replaced")
    assert d.resize(9, 40, wait=False) == "[resized]"
    assert _body(d._render_diff(s)) == (
        "[row 2]\nsecond line replaced\n[row 8 inserted]\n"
    )


def test_a_cleared_baseline_shows_everything_then_diffs_against_it() -> None:
    d, s = _driver_on_a_fake_screen()
    s.feed(b"\r\n".join(b"line %d of the screen" % i for i in range(8)))
    d.view()
    d._shown = None
    assert _body(d._render_diff(s)) == (
        "[full screen]\n" + "\n".join(f"line {i} of the screen" for i in range(8)))
    s.feed(b"\x1b[4;1H\x1b[2Kthird line replaced")
    assert _body(d._render_diff(s)) == "[row 3]\nthird line replaced"


def test_start_resets_the_baseline_between_sessions(monkeypatch) -> None:
    first = _FakeSession()
    first_rows = [f"line {i} from first program" for i in range(8)]
    first.feed("\r\n".join(first_rows).encode())

    second = _FakeSession()
    second_rows = list(first_rows)
    second_rows[4] = "one row from second program"
    second.feed("\r\n".join(second_rows).encode())

    sessions = iter((first, second))
    monkeypatch.setattr(
        "impty.driver.TerminalSession",
        lambda *_args, **_kwargs: next(sessions),
    )

    d = ImptyDriver()
    d.start(["first"])
    first.returncode = 0
    rendered = d.start(["second"])

    assert _body(rendered) == "[full screen]\n" + _screenful(*second_rows)
    second.feed(b"\x1b[7;1H\x1b[2Kanother second-program row")
    assert _body(d._render_diff(second)) == (
        "[row 6]\nanother second-program row")


def test_send_returns_only_the_rows_its_reaction_changed() -> None:
    d, s = _driver_on_a_fake_screen()
    s.feed(b"\r\n".join(b"line %d of the screen" % i for i in range(8)))
    d.view()
    s.feed(b"\x1b[5;1H\x1b[2Kfourth line replaced")
    assert _body(d.send("x", wait=0.0)) == "[row 4]\nfourth line replaced"


def test_send_signal_returns_only_the_rows_its_reaction_changed() -> None:
    d, s = _driver_on_a_fake_screen()
    s.feed(b"\r\n".join(b"line %d of the screen" % i for i in range(8)))
    d.view()
    s.feed(b"\x1b[7;1H\x1b[2Ksixth line replaced")
    assert _body(d.send_signal()) == "[row 6]\nsixth line replaced"


def test_resize_returns_only_the_rows_changed_by_geometry() -> None:
    d, s = _driver_on_a_fake_screen(cols=40, rows=8)
    s.feed(b"\r\n".join(b"line %d of the screen" % i for i in range(8)))
    d.view()
    assert _body(d.resize(9, 40, wait=0.0)) == "[row 8 inserted]\n"

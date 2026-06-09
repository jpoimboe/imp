# tests/test_line_semantics.py
"""lines.py: \n-only splitting + cat -n counting rules shared by read_file
and grep (tool-ergonomics spec)."""
from __future__ import annotations

from pathlib import Path

from imp.agents.tools.limits import FILE_READ_CAP_BYTES, MATCH_LINE_CAP_BYTES
from imp.agents.tools.lines import file_is_cap_truncated, split_lines, truncate_line


def test_terminal_newline_phantom_is_not_a_line() -> None:
    assert split_lines("a\nb\n", cap_truncated=False) == ["a", "b"]


def test_unterminated_final_line_is_a_line() -> None:
    assert split_lines("a\nb", cap_truncated=False) == ["a", "b"]


def test_empty_text_has_no_lines() -> None:
    assert split_lines("", cap_truncated=False) == []


def test_formfeed_and_friends_are_line_content_not_breaks() -> None:
    # str.splitlines() would split on \f, \v, \x85; \n-only splitting must not
    # — numbering must agree with every \n-counting consumer.
    assert split_lines("a\fb\nc\x85d\vE", cap_truncated=False) == ["a\fb", "c\x85d\vE"]


def test_crlf_keeps_carriage_return_as_content() -> None:
    assert split_lines("foo\r\nbar\r\n", cap_truncated=False) == ["foo\r", "bar\r"]


def test_cap_truncated_drops_trailing_partial_fragment() -> None:
    # The read cap cut mid-line: the fragment is not a readable line; matching
    # or rendering it would fabricate one.
    assert split_lines("a\nb\npartia", cap_truncated=True) == ["a", "b"]


def test_cap_truncated_on_newline_keeps_last_real_line() -> None:
    # Cap landed exactly ON a \n: the trailing split element is the phantom ""
    # (already removed); the partial-drop rule must NOT also fire, or it
    # deletes a complete, real final readable line.
    assert split_lines("a\nb\n", cap_truncated=True) == ["a", "b"]


def test_file_is_cap_truncated_exactly_at_cap_is_complete(tmp_path: Path) -> None:
    # truncated=True from the reader but st_size <= cap: nothing was dropped.
    f = tmp_path / "exact.bin"
    f.write_bytes(b"x" * 10)
    assert file_is_cap_truncated(True, f) is False
    assert file_is_cap_truncated(False, f) is False


def test_file_is_cap_truncated_true_when_bigger_than_cap(tmp_path: Path) -> None:
    big = tmp_path / "big.bin"
    with big.open("wb") as f:
        f.seek(FILE_READ_CAP_BYTES)  # sparse file: st_size = cap + 1, no 5 MB I/O
        f.write(b"x")
    assert big.stat().st_size == FILE_READ_CAP_BYTES + 1
    assert file_is_cap_truncated(True, big) is True


def test_file_is_cap_truncated_vanished_file_split(tmp_path: Path) -> None:
    # Vanished between read and stat. truncated=False: the read saw EOF —
    # complete. truncated=True: the read did NOT see EOF — calling it complete
    # would retain the partial fragment and fabricate a total.
    gone = tmp_path / "never_created.bin"
    assert file_is_cap_truncated(False, gone) is False
    assert file_is_cap_truncated(True, gone) is True


def test_truncate_line_caps_content_with_marker() -> None:
    long_line = "x" * (MATCH_LINE_CAP_BYTES + 5)
    assert truncate_line(long_line) == "x" * MATCH_LINE_CAP_BYTES + "…[truncated]"
    assert truncate_line("short") == "short"

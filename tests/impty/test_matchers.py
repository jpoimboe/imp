from __future__ import annotations

import pytest

from tests.impty.matchers import assert_screen


class _StubView:
    def __init__(self, rows: list[str]) -> None:
        self._rows = rows

    def lines(self, limit: int | None = None) -> list[str]:
        return self._rows if limit is None else self._rows[-limit:]


def test_contains_and_excludes_and_patterns_pass_on_a_match():
    view = _StubView(["spec complete", "command=echo 3", "call burst"])
    assert_screen(view,
                  contains=("spec complete", "command=echo"),
                  excludes=("echo 0",),
                  patterns=(r"command=echo \d",))


def test_missing_substring_raises_assertionerror():
    view = _StubView(["only this line"])
    with pytest.raises(AssertionError):
        assert_screen(view, contains=("not present",))


def test_present_exclude_raises_assertionerror():
    view = _StubView(["command=echo 0"])
    with pytest.raises(AssertionError):
        assert_screen(view, excludes=("command=echo",))


def test_unmatched_pattern_raises_assertionerror():
    view = _StubView(["no digits here"])
    with pytest.raises(AssertionError):
        assert_screen(view, patterns=(r"\d+",))


def test_limit_restricts_the_view():
    view = _StubView(["row0", "row1", "row2"])
    assert_screen(view, contains=("row1", "row2"), excludes=("row0",), limit=2)

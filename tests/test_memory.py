"""imp/memory/ filesystem memory: comment stripping, snapshot loading, and
broad->narrow resolution (spec hede92 slice 1)."""
from __future__ import annotations

from pathlib import Path

from imp.providers.memory import (
    load_memory_snapshot,
    resolve_memory,
    strip_html_comments,
)


def test_strip_removes_single_line_comment() -> None:
    assert strip_html_comments("a<!-- drop me -->b") == "ab"


def test_strip_removes_multiline_comment() -> None:
    assert strip_html_comments("x<!--\nmany\nlines\n-->y") == "xy"


def test_strip_removes_multiple_comments() -> None:
    assert strip_html_comments("<!--a-->keep<!--b-->this") == "keepthis"


def test_strip_leaves_hash_headings_untouched() -> None:
    # '#' is a markdown heading, NOT a comment.
    assert strip_html_comments("# Heading\ntext") == "# Heading\ntext"


def test_strip_no_comment_is_identity() -> None:
    assert strip_html_comments("plain prose") == "plain prose"

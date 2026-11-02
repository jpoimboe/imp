from __future__ import annotations

import re
from datetime import UTC, datetime

from imp.cli.main import (
    _excerpts,
    _highlight,
    _render_search_hit,
    _search_list_line,
    _spine_segment,
)
from imp.cli.store import SearchHit as _SearchHit

_NOW = datetime(2026, 7, 13, tzinfo=UTC)


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_highlight_plain_when_color_off_is_identity() -> None:
    assert _highlight("the dark night", "dark", color=False) == "the dark night"


def test_highlight_wraps_every_occurrence_when_color_on() -> None:
    out = _highlight("dark and dark", "dark", color=True)
    assert "\x1b[" in out
    assert out.count("\x1b[0m") == 2
    assert _plain(out) == "dark and dark"


def test_highlight_treats_query_metacharacters_literally() -> None:
    out = _highlight("a.b and axb", "a.b", color=True)
    assert out.count("\x1b[0m") == 1
    assert _plain(out) == "a.b and axb"


def test_spine_segment_plain_when_color_off() -> None:
    assert _spine_segment("darkid", "dark", "wordmark", color=False) == "darkid"


def test_spine_segment_reopens_base_color_after_highlight() -> None:
    out = _spine_segment("darkid", "dark", "wordmark", color=True)
    assert _plain(out) == "darkid"
    assert out.count("\x1b[0m") == 2
    # wordmark SGR re-opens for the trailing "id"; a bare uncolored reset drops it.
    assert out.count("\x1b[33m") >= 2


def test_search_list_line_spine_plain() -> None:
    hit = _SearchHit("abc", "Dark title", "active", "body", "2026-07-10T00:00:00+00:00", None)
    line = _search_list_line(hit, query="dark", now=_NOW, color=False)
    assert line == "abc · Dark title · active · 3d"


def test_search_list_line_untitled_drops_title_segment() -> None:
    hit = _SearchHit("darkid", "", "backlog", "", None, None)
    line = _search_list_line(hit, query="darkid", now=_NOW, color=False)
    assert line == "darkid · backlog · -"
    assert " ·  · " not in line


def test_search_list_line_colorized_heading() -> None:
    hit = _SearchHit("darkid", "Dark mode", "active", "body",
                     "2026-07-10T00:00:00+00:00", None)
    line = _search_list_line(hit, query="dark", now=_NOW, color=True)
    assert "\x1b[" in line
    assert _plain(line) == "darkid · Dark mode · active · 3d"
    assert "\x1b[1m\x1b[33m" in line          # bold+yellow query highlight


def test_render_hit_heading_then_two_space_excerpts() -> None:
    hit = _SearchHit("abc", "Title", "active", "please wire a 50% budget cap",
                     "2026-07-10T00:00:00+00:00", None)
    lines = _render_search_hit(hit, "50%", now=_NOW, color=False)
    assert lines[0] == "abc · Title · active · 3d"
    assert any(line.startswith("  ") and "50%" in line for line in lines[1:])
    assert not any(line.startswith("    ") for line in lines[1:])


def test_render_hit_no_match_lines_when_only_heading_matches() -> None:
    hit = _SearchHit("arcid", "Dark mode", "archive", "", None, None)
    lines = _render_search_hit(hit, "dark", now=_NOW, color=False)
    assert lines == ["arcid · Dark mode · archive · -"]
    assert not any("matched on" in line for line in lines)


def test_render_hit_adds_more_line_beyond_three_occurrences() -> None:
    hit = _SearchHit("abc", "Title", "active", "query " * 5, None, None)
    lines = _render_search_hit(hit, "query", now=_NOW, color=False)
    assert len(lines) == 1 + 3 + 1
    assert lines[-1] == "  … +2 more"


def test_excerpts_span_surrounds_match_with_ellipses() -> None:
    content = "x" * 200 + " the 50% mark is here " + "y" * 200
    spans, extra = _excerpts(content, "50%", color=False)
    assert len(spans) == 1
    assert extra == 0
    assert "50%" in spans[0]
    assert spans[0].startswith("… ")
    assert spans[0].endswith(" …")


def test_excerpts_collapse_newlines_and_cap_limit() -> None:
    content = "\n".join(f"line {i} has query token" for i in range(10))
    spans, extra = _excerpts(content, "query", color=False)
    assert len(spans) == 3
    assert extra == 7
    assert all("\n" not in s for s in spans)


def test_excerpts_empty_when_no_match() -> None:
    assert _excerpts("nothing relevant here", "zzz", color=False) == ([], 0)


def test_excerpts_empty_query_returns_empty_not_infinite_loop() -> None:
    assert _excerpts("any content here", "", color=False) == ([], 0)

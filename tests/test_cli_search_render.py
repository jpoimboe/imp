from __future__ import annotations

from imp.cli.main import (
    _excerpts,
    _highlight,
    _render_search_hit,
    _search_list_line,
    _SearchHit,
)


def test_highlight_plain_when_color_off_is_identity() -> None:
    assert _highlight("the dark night", "dark", color=False) == "the dark night"


def test_highlight_wraps_every_occurrence_when_color_on() -> None:
    out = _highlight("dark and dark", "dark", color=True)
    assert "\x1b[" in out                       # ANSI escapes injected
    assert out.count("\x1b[0m") == 2            # one reset per occurrence
    # Stripping every SGR escape restores the original text.
    import re
    assert re.sub(r"\x1b\[[0-9;]*m", "", out) == "dark and dark"


def test_excerpts_span_surrounds_match_with_ellipses() -> None:
    content = "x" * 100 + " the 50% mark is here " + "y" * 100
    spans = _excerpts(content, "50%", color=False, context=10)
    assert len(spans) == 1
    assert "50%" in spans[0]
    assert spans[0].startswith("… ")           # left context was truncated
    assert spans[0].endswith(" …")             # right context was truncated


def test_excerpts_collapse_newlines_and_cap_limit() -> None:
    content = "\n".join(f"line {i} has query token" for i in range(10))
    spans = _excerpts(content, "query", color=False, limit=3)
    assert len(spans) == 3                      # capped at limit
    assert all("\n" not in s for s in spans)    # whitespace collapsed


def test_excerpts_empty_when_no_match() -> None:
    assert _excerpts("nothing relevant here", "zzz", color=False) == []


def test_render_hit_shows_excerpts_for_body_match() -> None:
    hit = _SearchHit("abc", "Title", "active", "please wire a 50% budget cap")
    lines = _render_search_hit(hit, "50%", color=False)
    assert lines[0] == "abc  Title  [active]"
    assert any(line.startswith("    ") and "50%" in line for line in lines[1:])


def test_render_hit_notes_matched_field_when_no_content_span() -> None:
    # id "arcid" deliberately does NOT contain the query, so the title branch
    # is genuinely exercised (a query that also matched the id would report id).
    hit = _SearchHit("arcid", "Dark mode", "archive", "")
    lines = _render_search_hit(hit, "dark", color=False)
    assert lines[0] == "arcid  Dark mode  [archive]"
    assert lines[1] == "    (matched on title)"


def test_render_hit_untitled_placeholder_and_id_field_note() -> None:
    hit = _SearchHit("darkid", "", "backlog", "")
    lines = _render_search_hit(hit, "darkid", color=False)
    assert lines[0] == "darkid  (untitled)  [backlog]"
    assert lines[1] == "    (matched on id)"


def test_search_list_line_is_terse() -> None:
    hit = _SearchHit("abc", "Title", "active", "body body body")
    assert _search_list_line(hit) == "abc  Title  [active]"


def test_excerpts_empty_query_returns_empty_not_infinite_loop() -> None:
    assert _excerpts("any content here", "", color=False) == []


def test_highlight_treats_query_metacharacters_literally() -> None:
    import re
    # A '.' in the query must match a literal dot, not regex "any char" — so
    # "a.b" is highlighted but "axb" is not.
    out = _highlight("a.b and axb", "a.b", color=True)
    assert out.count("\x1b[0m") == 1                       # only the literal match wrapped
    assert re.sub(r"\x1b\[[0-9;]*m", "", out) == "a.b and axb"

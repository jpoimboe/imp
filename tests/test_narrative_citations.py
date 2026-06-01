from __future__ import annotations

from imp.cli.narrative import _format_citations_footer


def test_footer_url_only() -> None:
    out = _format_citations_footer([{"url": "https://a.test"}])
    assert out == "\n\nSources:\n- https://a.test"


def test_footer_url_and_title() -> None:
    out = _format_citations_footer(
        [{"url": "https://a.test", "title": "A Title"}])
    assert out == "\n\nSources:\n- A Title — https://a.test"


def test_footer_empty_or_none_is_blank() -> None:
    assert _format_citations_footer(None) == ""
    assert _format_citations_footer([]) == ""


def test_footer_skips_entries_without_url() -> None:
    out = _format_citations_footer([{"title": "no url"}, {"url": "https://b.test"}])
    assert out == "\n\nSources:\n- https://b.test"

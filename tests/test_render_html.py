from __future__ import annotations

import importlib.resources as ir

from imp.cli.render_html import _md_to_fragment


def test_github_markdown_css_asset_is_bundled() -> None:
    css = (ir.files("imp") / "assets" / "github-markdown.css").read_text(
        encoding="utf-8")
    assert ".markdown-body" in css          # the class github-markdown-css targets
    assert "color-scheme: light" in css     # the *light* theme was vendored
    assert len(css) > 1000                  # a real stylesheet, not a stub


def test_fragment_keeps_content_raw_html_inert() -> None:
    # Prose the Part 1 renderer already HTML-escaped stays escaped through
    # markdown-it (html disabled) — the entity round-trips, never a live tag.
    frag = _md_to_fragment("&lt;script&gt;alert(1)&lt;/script&gt;")
    assert "&lt;script&gt;" in frag
    assert "<script>" not in frag


def test_fragment_renders_fenced_hostile_content_inert() -> None:
    frag = _md_to_fragment("```\n<script>alert(1)</script>\n```")
    assert "<pre>" in frag                   # a code block, not raw HTML
    assert "&lt;script&gt;" in frag          # escaped by the code renderer
    assert "<script>" not in frag


def test_fragment_assembles_details_fold_as_real_tags() -> None:
    md = "<details>\n<summary>thinking</summary>\n\npondering deeply\n\n</details>"
    frag = _md_to_fragment(md)
    assert "<details>" in frag               # a real fold element…
    assert "<summary>thinking</summary>" in frag
    assert "&lt;details&gt;" not in frag     # …NOT passed through markdown-it as text
    assert "pondering deeply" in frag        # inner content rendered


def test_fragment_renders_pipe_table() -> None:
    md = "| Step | Duration |\n|---|---|\n| spec | 1m00s |"
    frag = _md_to_fragment(md)
    assert "<table>" in frag                 # GFM table plugin is enabled
    assert "<td>spec</td>" in frag


def test_fold_inner_fence_with_details_marker_is_not_split_early() -> None:
    # A tool result inside the fold's code fence contains a literal `</details>`
    # line. The fence-aware splitter must treat it as fenced content, never as the
    # fold's closing tag — a naive line splitter would close the fold early.
    md = (
        "<details>\n"
        "<summary>1 call</summary>\n"
        "\n"
        "```\n"
        "· bash\n"
        "  command=cat x\n"
        "</details>\n"
        "tail-line\n"
        "```\n"
        "\n"
        "</details>"
    )
    frag = _md_to_fragment(md)
    assert frag.count("<details>") == 1      # exactly one real fold
    fold_end = frag.rindex("</details>")
    # both the fake close marker and the line after it must live INSIDE the fold's
    # code block — a naive fence-unaware splitter would push them past the close.
    assert "&lt;/details&gt;" in frag        # the fake close is inert fenced text
    assert frag.index("&lt;/details&gt;") < fold_end
    assert "tail-line" in frag               # content after the fake marker survived
    assert frag.index("tail-line") < fold_end

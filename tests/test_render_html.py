from __future__ import annotations

import importlib.resources as ir

from imp.cli import narrative as nv
from imp.cli.render_html import _md_to_fragment, render_html
from imp.cli.render_markdown import render_markdown


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


def _ev(eid, etype, agent_id=None, **payload):
    return {"id": eid, "agent_id": agent_id, "type": etype, "payload": payload}


def test_render_html_is_self_contained_document() -> None:
    out = render_html("# Title\n\nsome **bold** prose\n")
    assert out.startswith("<!DOCTYPE html>")
    assert '<body class="markdown-body">' in out
    assert "<style>" in out
    assert "color-scheme: light" in out        # vendored CSS inlined into <head>
    assert "<h1>Title</h1>" in out             # the markdown body rendered
    assert "<strong>bold</strong>" in out
    # a single offline file — no external references
    assert "<link" not in out
    assert 'href="http' not in out
    assert 'src="http' not in out


def test_render_html_end_to_end_neutralizes_hostile_tool_result() -> None:
    events = [
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("4", "tool_call", agent_id="step:p/s", id="b1", name="bash",
            input={"command": "cat evil"}),
        _ev("5", "tool_result", agent_id="step:p/s", id="b1",
            content="<script>alert(1)</script>\x00", error=False),
    ]
    tree = nv.build_tree(events)
    out = render_html(render_markdown(tree, full=True))
    assert "<script>alert(1)</script>" not in out   # never a live tag
    assert "&lt;script&gt;" in out                  # escaped / inert
    assert "\x00" not in out                        # control byte neutralized (-> ^@)
    assert "^@" in out

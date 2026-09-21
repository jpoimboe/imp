from __future__ import annotations

import importlib.resources as ir

from imp.cli import narrative as nv
from imp.cli.accounting import RowUsage
from imp.cli.quota_column import QuotaColumn
from imp.cli.quota_share import QuotaFigure, QuotaWithheld, Units, Withheld
from imp.cli.render_html import _md_to_fragment, render_html
from imp.cli.render_markdown import render_markdown, render_summary_markdown
from imp.cli.render_summary import StepRow, SummaryHeader


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


def test_fold_nested_details_depth_aware() -> None:
    # An inner fold inside an outer fold must not close the outer fold early.
    inner_fold = (
        "<details>\n<summary>thinking</summary>\n\npondering deeply\n\n</details>"
    )
    md = (
        "<details>\n<summary>outer</summary>\n\n"
        + inner_fold
        + "\n\nafter inner\n\n</details>"
    )
    frag = _md_to_fragment(md)
    assert frag.count("<details>") == 2        # two real folds
    assert "&lt;details&gt;" not in frag       # no escaped open tags
    assert "&lt;/details&gt;" not in frag      # no escaped close tags
    outer_fold_end = frag.rindex("</details>")
    assert "after inner" in frag
    assert frag.index("after inner") < outer_fold_end  # trailing line inside outer fold


def test_nested_subagent_thinking_fold_html_no_leaked_close_tag() -> None:
    # A shown subagent containing a shown thinking fold produces nested <details>.
    # The HTML converter must not close the outer fold at the inner fold's </details>,
    # leaking the subagent's outcome and its own closing tag as visible text.
    events = [
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("th1", "thinking", agent_id="sub:tk1", content="pondering deeply"),
        _ev("su1", "agent_message", agent_id="sub:tk1", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "looks good"}]}),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content='{"status":"Success","summary":"all clear"}', error=False),
    ]
    tree = nv.build_tree(events)
    md = render_markdown(tree, full=False, show=frozenset({"subagents", "thinking"}))
    # Confirm the markdown really nests folds (thinking inside subagent)
    assert md.count("<details>") == 2
    out = render_html(md)
    assert "&lt;/details&gt;" not in out   # no leaked closing tag as visible text
    # outcome must sit INSIDE the subagent fold, not spill out after it
    outer_fold_end = out.rindex("</details>")
    assert "all clear" in out
    assert out.index("all clear") < outer_fold_end


def test_fold_inner_info_string_fence_does_not_invert_fold_tracking() -> None:
    # A closing ``` after an info-string opener (```python) must not be treated
    # as a new fence opener; otherwise the second fold's markers are swallowed.
    md = (
        "<details>\n"
        "<summary>first</summary>\n"
        "\n"
        "```python\n"
        "def f(): ...\n"
        "```\n"
        "\n"
        "</details>\n"
        "<details>\n"
        "<summary>second</summary>\n"
        "\n"
        "also rendered\n"
        "\n"
        "</details>"
    )
    frag = _md_to_fragment(md)
    assert frag.count("<details>") == 2          # both folds are real elements
    assert "<pre>" in frag                        # code block rendered
    assert "</code>" in frag
    assert "&lt;details&gt;" not in frag          # no escaped open tags
    assert "&lt;/details&gt;" not in frag         # no escaped close tags
    assert "also rendered" in frag


def test_info_string_fence_in_thinking_does_not_swallow_later_fold() -> None:
    # Thinking prose with an info-string code block (```python...```) reaches
    # the HTML converter as live markdown (render_markdown escapes <> but not
    # backticks). The closing ``` must not invert fence state and swallow the
    # second subagent fold's markers as visible &lt;details&gt; text.
    events = [
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("th1", "thinking", agent_id="sub:tk1",
            content="Here is code:\n\n```python\ndef f(): ...\n```\n\ndone"),
        _ev("su1", "agent_message", agent_id="sub:tk1", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "looks good"}]}),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content='{"status":"Success","summary":"all clear"}', error=False),
        _ev("tk2", "tool_call", agent_id="step:p/s", id="b2", name="sub_agent",
            input={"subagent_name": "reviewer2", "task_description": "go2"}),
        _ev("su2", "agent_message", agent_id="sub:tk2", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "second done"}]}),
        _ev("r2", "tool_result", agent_id="step:p/s", id="b2",
            content='{"status":"Success","summary":"second clear"}', error=False),
    ]
    tree = nv.build_tree(events)
    md = render_markdown(tree, full=False, show=frozenset({"subagents", "thinking"}))
    out = render_html(md)
    assert "&lt;/details&gt;" not in out          # no leaked closing tags
    assert "&lt;details&gt;" not in out           # no leaked open tags
    assert "second done" in out                   # second fold's content rendered
    assert out.count("<details>") >= 2            # second fold is a real element


def test_tilde_fence_in_prose_does_not_swallow_a_later_fold() -> None:
    # A ``` line inside a ~~~ fence is literal content to CommonMark; treating
    # it as a fence opener would leave the scan fenced for the rest of the doc.
    md = ("prose\n\n~~~\n```\n~~~\n\n<details>\n<summary>s</summary>\n\n"
          "body\n\n</details>\n")
    frag = _md_to_fragment(md)
    assert frag.count("<details>") == 1
    assert "&lt;details&gt;" not in frag
    assert "body" in frag


def test_indented_fence_in_prose_does_not_swallow_a_later_fold() -> None:
    # Four-space-indented ``` is an indented code block, not a fence opener.
    md = ("prose\n\n    ```\n    indented\n\n<details>\n<summary>s</summary>\n\n"
          "body\n\n</details>\n")
    frag = _md_to_fragment(md)
    assert frag.count("<details>") == 1
    assert "&lt;details&gt;" not in frag
    assert "body" in frag


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


def test_render_html_indents_nested_folds() -> None:
    md = ("<details>\n<summary>outer</summary>\n\n"
          "<details>\n<summary>inner</summary>\n\nbody\n\n</details>\n\n"
          "</details>\n")
    out = render_html(md)
    assert out.count("<details>") == 2          # both folds are real elements
    assert ".markdown-body details details { margin-left: 1.5em; }" in out


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


def test_quiet_first_retry_is_absent_from_the_lean_html_export() -> None:
    tree = nv.build_tree([
        {"id": "r1", "agent_id": None, "type": "agent_retry",
         "payload": {"category": "auth", "attempt": 1, "backoff_seconds": 1.0}}])
    assert "retry 1" not in render_html(render_markdown(tree, full=False))
    assert "retry 1" in render_html(render_markdown(tree, full=True))


def test_agent_prose_bare_sgr_fence_html_export_is_clean() -> None:
    bare = ("[38;2;255;154;60mwarning: [auth] retry 2 (1.0s) — another session "
            "rotated the OAuth token; retrying with the new one[0m")
    tree = nv.build_tree([
        {"id": "2", "agent_id": None, "type": "step_started",
         "payload": {"step": "s", "kind": "agent", "phase": "p",
                     "model": "m", "provider": "anthropic"}},
        {"id": "3", "agent_id": "step:p/s", "type": "agent_message",
         "payload": {"provider": "anthropic", "model": "m",
                     "stop_reason": "end_turn", "usage": {},
                     "raw": {"content": [{"type": "text",
                                          "text": f"Look:\n\n```\n{bare}\n```\n"}]}}},
    ])
    out = render_html(render_markdown(tree, full=False))
    assert "38;2;255;154;60" not in out
    assert "\x1b" not in out
    assert "^[[" not in out
    assert "warning: [auth] retry 2 (1.0s)" in out


def test_agent_prose_colored_inline_span_html_export_is_plain_code() -> None:
    tree = nv.build_tree([
        {"id": "2", "agent_id": None, "type": "step_started",
         "payload": {"step": "s", "kind": "agent", "phase": "p",
                     "model": "m", "provider": "anthropic"}},
        {"id": "3", "agent_id": "step:p/s", "type": "agent_message",
         "payload": {"provider": "anthropic", "model": "m",
                     "stop_reason": "end_turn", "usage": {},
                     "raw": {"content": [
                         {"type": "text",
                          "text": "The gauge reads `[1;33m78%[0m` amber.\n"}]}}},
    ])
    out = render_html(render_markdown(tree, full=False))
    assert "<code>78%</code>" in out     # a real <code> element, not literal codes
    assert "[1;33m" not in out
    assert "\x1b" not in out


def test_summary_html_export_keeps_the_notional_sigil_out_of_a_list() -> None:
    # A legend line opening `* ` is a Markdown bullet: unescaped, markdown-it
    # eats the very asterisk the line exists to explain.
    rows = [
        StepRow(phase="p", step="metered", start_ts="2026-01-01T00:00:00+00:00",
                end_ts="2026-01-01T00:00:30+00:00",
                usage=RowUsage(fresh_input=100, output=10, dollars=2.00,
                               priced=1)),
        StepRow(phase="p", step="subscription",
                start_ts="2026-01-01T00:00:30+00:00",
                end_ts="2026-01-01T00:01:00+00:00",
                usage=RowUsage(fresh_input=100, output=10, dollars=4.00,
                               priced=1, notional=1)),
    ]
    hdr = SummaryHeader(workflow="f", quest_id="c", status="merged",
                        total_seconds=60.0)
    out = render_html(render_summary_markdown(rows, header=hdr))
    assert "<td>$6.00*</td>" in out                     # the total's starred cell
    assert ("<p>* theoretical API list price; actual subscription usage is"
            " much lower</p>") in out
    assert "<li>theoretical API list price" not in out  # never a bullet


def test_summary_html_export_carries_the_weekly_column_and_its_footnote() -> None:
    # AC 19(a): render_html is a pure Markdown pass, so the column arrives with
    # no work of its own — but the footnote's `*` must stay a paragraph.
    rows = [
        StepRow(phase="p", step="plan-writer",
                start_ts="2026-01-01T00:00:00+00:00",
                end_ts="2026-01-01T00:01:30+00:00",
                usage=RowUsage(fresh_input=1_000_000, output=20_000,
                               dollars=3.00, priced=1, notional=1),
                units=Units.of("claude", 2.0)),
    ]
    hdr = SummaryHeader(workflow="f", quest_id="c", status="merged",
                        total_seconds=60.0)
    column = QuotaColumn((QuotaFigure("claude", 9.0, 2.0, 10.0),))
    out = render_html(render_summary_markdown(rows, header=hdr, quota=column))
    assert "<th>Weekly*</th>" in out
    assert "<td>1.80%</td>" in out               # 9 points x 2/10 units
    assert ("<p>* estimated to ±1 point of the weekly gauge; usage outside imp"
            " during the run skews it high</p>") in out
    assert "<li>estimated to ±1 point" not in out   # never a bullet
    assert "$" not in out                           # AC 13: no dollar figure


def test_summary_html_puts_a_providers_share_in_the_quota_column() -> None:
    # AC 14(a)/(e) on the export: a breakdown row one cell short of the header
    # pads at the end, which lands the share in the `Out` column and leaves
    # `Weekly*` empty — only the rendered table shows which cell it reached.
    rows = [
        StepRow(phase="plan", step="plan-writer",
                start_ts="2026-01-01T00:00:00+00:00",
                end_ts="2026-01-01T00:01:30+00:00",
                usage=RowUsage(fresh_input=1_000_000, output=20_000,
                               dollars=3.0, priced=1, notional=1),
                units=Units.of("claude", 2.0)),
        StepRow(phase="plan", step="coder",
                start_ts="2026-01-01T00:01:30+00:00",
                end_ts="2026-01-01T00:02:30+00:00",
                usage=RowUsage(fresh_input=200_000, output=3_000, dollars=1.0,
                               priced=1, notional=1),
                units=Units.of("codex", 1.0)),
    ]
    hdr = SummaryHeader(workflow="f", quest_id="c", status="merged",
                        total_seconds=60.0)
    column = QuotaColumn((QuotaFigure("claude", 4.0, 2.0, 4.0),
                          QuotaWithheld("codex", Withheld.BELOW_RESOLUTION)))
    out = render_html(render_summary_markdown(rows, header=hdr, quota=column))
    assert ("<tr>\n<td>↳ claude</td>\n<td></td>\n<td></td>\n<td></td>\n"
            "<td></td>\n<td></td>\n<td>2.00%</td>\n</tr>") in out
    assert ("<tr>\n<td>↳ codex</td>\n<td></td>\n<td></td>\n<td></td>\n"
            "<td></td>\n<td></td>\n<td>—</td>\n</tr>") in out

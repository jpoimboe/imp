from __future__ import annotations

from imp.agents.reflow import reflow_markdown


def test_empty_input_returns_empty():
    assert reflow_markdown("") == ""


def test_wrapped_paragraph_joins_to_one_line():
    src = "alpha beta\ngamma delta"
    assert reflow_markdown(src) == "alpha beta gamma delta"


def test_blank_line_separates_paragraphs():
    src = "one two\nthree\n\nfour five\nsix"
    assert reflow_markdown(src) == "one two three\n\nfour five six"


def test_runs_of_blank_lines_preserved():
    src = "a\n\n\n\nb"
    assert reflow_markdown(src) == "a\n\n\n\nb"


def test_fenced_code_block_preserved_verbatim():
    src = "```python\nx = 1\ny = 2\n```"
    assert reflow_markdown(src) == src


def test_no_line_inside_fence_is_joined_even_if_prose():
    src = "```\nthis looks like\nwrapped prose\n```"
    assert reflow_markdown(src) == src


def test_tilde_fence_preserved():
    src = "~~~\nkeep\nthese\n~~~"
    assert reflow_markdown(src) == src


def test_pipe_table_rows_preserved():
    src = "| a | b |\n| - | - |\n| 1 | 2 |"
    assert reflow_markdown(src) == src


def test_atx_heading_stays_on_its_own_line():
    src = "# Title\nbody one\nbody two"
    assert reflow_markdown(src) == "# Title\nbody one body two"


def test_horizontal_rule_preserved():
    src = "above\n\n---\n\nbelow"
    assert reflow_markdown(src) == "above\n\n---\n\nbelow"


def test_blockquote_lines_preserved():
    src = "> quoted one\n> quoted two"
    assert reflow_markdown(src) == "> quoted one\n> quoted two"


def test_list_item_joins_continuation_but_items_stay_separate():
    src = " - first item\n   wrapped on\n - second item"
    assert reflow_markdown(src) == " - first item wrapped on\n - second item"


def test_ordered_list_markers_stay_separate():
    src = "1. one\n   more\n2. two"
    assert reflow_markdown(src) == "1. one more\n2. two"


def test_nested_list_continuation_joins_under_nested_marker():
    src = "- parent\n  - nested item\n    wrapped continuation\n- sibling"
    assert reflow_markdown(src) == (
        "- parent\n  - nested item wrapped continuation\n- sibling"
    )


def test_hard_break_double_space_suppresses_join():
    src = "line one  \nline two"
    assert reflow_markdown(src) == "line one  \nline two"


def test_hard_break_backslash_suppresses_join():
    src = "line one\\\nline two"
    assert reflow_markdown(src) == "line one\\\nline two"


def test_mid_paragraph_hard_break_is_idempotent():
    # A hard break in the MIDDLE of a wrapped paragraph: the break ends the join
    # and its trailing double-space is preserved, so a second pass is a no-op.
    src = "a\nb  \nc"
    once = reflow_markdown(src)
    assert once == "a b  \nc"
    assert reflow_markdown(once) == once


def test_idempotent():
    src = "a b\nc d\n\n - item one\n   cont\n\n```\ncode\n```"
    once = reflow_markdown(src)
    assert reflow_markdown(once) == once

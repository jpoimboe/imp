from datetime import UTC, datetime

import pytest

from imp.cli.change_row import (
    NAME_MIN,
    format_change_line,
    header_line,
    name_budget,
    relative_age,
)

_NOW = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "then_iso, expected",
    [
        ("2026-06-11T11:59:30+00:00", "30s"),   # 30 seconds ago
        ("2026-06-11T11:45:00+00:00", "15m"),   # 15 minutes ago
        ("2026-06-11T06:00:00+00:00", "6h"),    # 6 hours ago
        ("2026-06-08T12:00:00+00:00", "3d"),    # 3 days ago
        ("2026-06-11T13:00:00+00:00", "0s"),    # future clamps to 0s
        (None, "-"),                            # no timestamp
        ("", "-"),                              # empty
        ("not-a-date", "-"),                    # unparseable
    ],
)
def test_relative_age(then_iso, expected):
    assert relative_age(then_iso, now=_NOW) == expected


def _row(**kw):
    base = dict(change_id="yaitd7", title="Add version flag",
                status="running", step="code",
                last_iso="2026-06-11T11:45:00+00:00", now=_NOW)
    base.update(kw)
    return base


def test_format_columns_plain():
    line = format_change_line(**_row(), color=False)
    assert "\x1b[" not in line
    assert line.startswith("yaitd7")        # ID first
    assert "Add version flag" in line       # NAME
    assert "running" in line
    assert "code" in line
    assert "15m" in line                    # relative AGE


def test_name_is_final_column():
    line = format_change_line(**_row(), color=False)
    # NAME now trails every other column.
    assert line.index("Add version flag") > line.index("running")
    assert line.index("Add version flag") > line.index("code")
    assert line.index("Add version flag") > line.index("15m")


def test_format_unnamed_shows_id_and_unnamed_label():
    line = format_change_line(**_row(title="   "), color=False)
    assert line.startswith("yaitd7")
    assert "(unnamed)" in line


def test_header_line_leads_with_id_ends_with_name():
    h = header_line(color=False)
    assert h.startswith("ID")
    assert h.rstrip().endswith("NAME")
    assert "TITLE" not in h
    assert "HANDLE" not in h
    assert "STEP" not in h        # STEP column is merged into STATUS


def test_format_does_not_truncate_name_within_budget():
    line = format_change_line(**_row(title="x" * 40), color=False, name_width=48)
    assert "x" * 40 in line
    assert "…" not in line


def test_format_truncates_name_past_budget():
    line = format_change_line(**_row(title="x" * 40), color=False, name_width=24)
    # Truncated to 24 display cols: 23 chars + ellipsis.
    assert "x" * 23 + "…" in line
    assert "x" * 24 not in line


def test_format_status_color_running():
    line = format_change_line(**_row(status="running"), color=True)
    assert "\x1b[32mrunning\x1b[0m" in line


def test_format_dim_wraps_whole_row():
    line = format_change_line(**_row(status="completed"), color=True, dim=True)
    assert line.startswith("\x1b[2m")
    assert line.endswith("\x1b[0m")
    assert "\x1b[32m" not in line


def test_merged_status_renders_green():
    line = format_change_line(**_row(status="merged"), color=True)
    assert "\x1b[32mmerged\x1b[0m" in line


def test_name_budget_fills_remaining_width(monkeypatch):
    import os

    monkeypatch.setattr("shutil.get_terminal_size",
                        lambda fallback=(80, 24): os.terminal_size((120, 24)))
    # 120 cols - 51 fixed prefix = 69 left for the name.
    assert name_budget() == 69
    # An indent (e.g. the resume picker's marker) shrinks the budget.
    assert name_budget(indent=2) == 67


def test_name_budget_floors_at_minimum(monkeypatch):
    import os

    monkeypatch.setattr("shutil.get_terminal_size",
                        lambda fallback=(80, 24): os.terminal_size((20, 24)))
    assert name_budget() == NAME_MIN


def test_backlog_status_renders_dim():
    from imp.cli.change_row import STATUS_COLORS
    assert STATUS_COLORS["backlog"] == ("dim",)


def test_status_cell_merges_step_label_as_dim_suffix():
    line = format_change_line(**_row(status="running", step="write_spec"),
                              step_label="writing spec", color=True)
    # status word keeps its color; the label suffix is dim and parenthesized.
    assert "\x1b[32mrunning\x1b[0m" in line
    assert "\x1b[2m (writing spec)\x1b[0m" in line


def test_status_cell_falls_back_to_step_name_without_label():
    line = format_change_line(**_row(status="running", step="write_spec"),
                              step_label=None, color=False)
    assert "running (write_spec)" in line


def test_status_cell_bare_when_step_empty():
    # The default _row title ("Add version flag") has no paren, so a clean line
    # has none at all when the STATUS cell carries no ` (label)` suffix.
    line = format_change_line(**_row(status="completed", step="-"),
                              step_label=None, color=False)
    assert "completed" in line
    assert "(" not in line                     # no suffix in the STATUS cell


def test_draft_status_renders_step_suffix():
    # The draft special-case is gone: a non-empty step renders a ` (label)` suffix
    # like any other status, so a blocked Backlog draft can show its state.
    line = format_change_line(**_row(status="draft", step="blocked"),
                              step_label="blocked", color=False)
    assert "draft (blocked)" in line


def test_provenance_statuses_have_colors():
    from imp.cli.change_row import STATUS_COLORS
    assert "idea" in STATUS_COLORS
    assert "followup" in STATUS_COLORS


def test_status_word_never_truncated_only_label_is():
    line = format_change_line(
        **_row(status="merge_blocked", step="x"),
        step_label="resolving deferred items", color=False)
    assert "merge_blocked" in line               # status word intact
    assert "…" in line                           # label was truncated
    assert "resolving deferred items" not in line  # full label did not fit


def test_merge_blocked_status_word_is_wordmark_under_dark_theme() -> None:
    from imp.cli import palette as pal
    try:
        pal.set_theme("dark")
        line = format_change_line(**_row(status="merge_blocked", step="x"), color=True)
    finally:
        pal.set_theme("ansi")
    wordmark = pal.theme_escapes("dark")["wordmark"]   # \x1b[38;2;255;154;60m
    yellow = pal.theme_escapes("dark")["yellow"]        # \x1b[38;2;199;151;106m
    assert wordmark + "merge_blocked" in line           # status word painted wordmark
    assert yellow not in line                           # not the muted yellow role


def test_intent_fallback_for_empty_title():
    line = format_change_line(**_row(title="   ", status="running", step="s"),
                              intent="add a dark mode toggle", color=False)
    assert "add a dark mode toggle" in line
    assert "(unnamed)" not in line


def test_intent_fallback_for_empty_title_color():
    line = format_change_line(**_row(title="   ", status="running", step="s"),
                              intent="add a dark mode toggle", color=True)
    # intent text rendered dim
    assert "\x1b[2madd a dark mode toggle\x1b[0m" in line
    assert "(unnamed)" not in line


def test_intent_fallback_flattens_multiline_prompt():
    # A multi-line opening prompt (e.g. a pasted traceback) must collapse to a
    # single row; an embedded newline in the NAME cell would spill the table.
    line = format_change_line(
        **_row(title="", status="running", step="s"),
        intent="Ready to land the draft?\n  + Exception Group Traceback\n  |  File",
        color=False, name_width=80)
    assert "\n" not in line
    assert "Ready to land the draft? + Exception Group Traceback | File" in line


def test_unnamed_when_no_intent():
    line = format_change_line(**_row(title="   ", status="running", step="s"),
                              intent=None, color=False)
    assert "(unnamed)" in line


def test_status_colors_have_new_and_no_errored() -> None:
    from imp.cli.change_row import STATUS_COLORS
    assert "new" in STATUS_COLORS
    assert "errored" not in STATUS_COLORS

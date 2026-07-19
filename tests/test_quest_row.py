from datetime import UTC, datetime

import pytest

from imp.cli.quest_row import (
    NAME_MIN,
    format_quest_line,
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
    base = dict(quest_id="yaitd7", title="Add version flag",
                status="running", step="code",
                last_iso="2026-06-11T11:45:00+00:00", now=_NOW)
    base.update(kw)
    return base


def test_format_columns_plain():
    line = format_quest_line(**_row(), color=False)
    assert "\x1b[" not in line
    assert line.startswith("yaitd7")        # ID first
    assert "Add version flag" in line       # NAME
    assert "running" in line
    assert "code" in line
    assert "15m" in line                    # relative AGE


def test_name_is_second_column():
    line = format_quest_line(**_row(), color=False)
    # NAME now sits right after ID, before STATUS and UPDATED.
    assert line.index("Add version flag") < line.index("running")
    assert line.index("Add version flag") < line.index("code")
    assert line.index("Add version flag") < line.index("15m")
    assert line.index("yaitd7") < line.index("Add version flag")


def test_format_unnamed_shows_id_and_unnamed_label():
    line = format_quest_line(**_row(title="   "), color=False)
    assert line.startswith("yaitd7")
    assert "(unnamed)" in line


def test_header_line_leads_with_id_ends_with_updated():
    h = header_line(color=False, name_width=40)
    assert h.startswith("ID")
    assert h.rstrip().endswith("UPDATED")
    assert "NAME" in h            # NAME is the second column now
    assert h.index("NAME") < h.index("STATUS")
    assert "TITLE" not in h
    assert "HANDLE" not in h
    assert "STEP" not in h        # STEP column is merged into STATUS


def test_format_does_not_truncate_name_within_budget():
    line = format_quest_line(**_row(title="x" * 40), color=False, name_width=48)
    assert "x" * 40 in line
    assert "…" not in line


def test_format_truncates_name_past_budget():
    line = format_quest_line(**_row(title="x" * 40), color=False, name_width=24)
    # Truncated to 24 display cols: 23 chars + ellipsis.
    assert "x" * 23 + "…" in line
    assert "x" * 24 not in line


def test_format_status_color_running():
    line = format_quest_line(**_row(status="running"), color=True)
    assert "\x1b[1mrunning\x1b[0m" in line


def test_waiting_status_renders_wordmark():
    line = format_quest_line(**_row(status="waiting"), color=True)
    assert "\x1b[33mwaiting\x1b[0m" in line


def test_error_status_renders_red():
    line = format_quest_line(**_row(status="error"), color=True)
    assert "\x1b[31merror\x1b[0m" in line


def test_merge_blocked_renders_two_words_on_underscore_color_key():
    line = format_quest_line(**_row(status="merge_blocked"), color=True)
    assert "\x1b[33mmerge blocked\x1b[0m" in line   # wordmark color, two-word text
    assert "merge_blocked" not in line


def test_format_dim_wraps_whole_row():
    line = format_quest_line(**_row(status="completed"), color=True, dim=True)
    assert line.startswith("\x1b[2m")
    assert line.endswith("\x1b[0m")
    assert "\x1b[32m" not in line


def test_dim_row_column_order():
    # history (dim) path preserves the same column order: NAME then STATUS then UPDATED.
    line = format_quest_line(**_row(status="completed"), color=False, dim=True)
    assert line.index("Add version flag") < line.index("completed") < line.index("15m")


def test_merged_status_renders_plain():
    line = format_quest_line(**_row(status="merged"), color=True)
    assert "merged" in line
    assert "\x1b[3" not in line


def test_name_budget_fills_remaining_width(monkeypatch):
    import os

    monkeypatch.setattr("shutil.get_terminal_size",
                        lambda fallback=(80, 24): os.terminal_size((100, 24)))
    # 100 cols - 54 fixed prefix = 46 left for the name (below the 60 cap).
    assert name_budget() == 46
    # An indent (e.g. the resume picker's marker) shrinks the budget.
    assert name_budget(indent=2) == 44


def test_name_budget_caps_at_name_cap(monkeypatch):
    import os

    monkeypatch.setattr("shutil.get_terminal_size",
                        lambda fallback=(80, 24): os.terminal_size((200, 24)))
    # 200 - 54 = 146 available, but the NAME column is capped at NAME_CAP (60).
    assert name_budget() == 60
    # The indent is applied before the cap; here it is still absorbed by the cap.
    assert name_budget(indent=2) == 60


def test_header_column_reads_updated_not_age():
    h = header_line(color=False, name_width=40)
    assert "UPDATED" in h
    assert "AGE" not in h


def test_name_budget_floors_at_minimum(monkeypatch):
    import os

    monkeypatch.setattr("shutil.get_terminal_size",
                        lambda fallback=(80, 24): os.terminal_size((20, 24)))
    assert name_budget() == NAME_MIN



def test_status_cell_merges_step_label_as_dim_suffix():
    line = format_quest_line(**_row(status="running", step="write_spec"),
                              step_label="writing spec", color=True)
    # status word keeps its color; the label suffix is dim and parenthesized.
    assert "\x1b[1mrunning\x1b[0m" in line
    assert "\x1b[2m (writing spec)\x1b[0m" in line


def test_status_cell_falls_back_to_step_name_without_label():
    line = format_quest_line(**_row(status="running", step="write_spec"),
                              step_label=None, color=False)
    assert "running (write_spec)" in line


def test_status_cell_bare_when_step_empty():
    # The default _row title ("Add version flag") has no paren, so a clean line
    # has none at all when the STATUS cell carries no ` (label)` suffix.
    line = format_quest_line(**_row(status="completed", step="-"),
                              step_label=None, color=False)
    assert "completed" in line
    assert "(" not in line                     # no suffix in the STATUS cell


def test_draft_status_renders_step_suffix():
    # The draft special-case is gone: a non-empty step renders a ` (label)` suffix
    # like any other status, so a blocked Backlog draft can show its state.
    line = format_quest_line(**_row(status="draft", step="blocked"),
                              step_label="blocked", color=False)
    assert "draft (blocked)" in line


def test_provenance_words_absent_from_palette():
    from imp.cli.quest_row import STATUS_COLORS
    assert "idea" not in STATUS_COLORS
    assert "followup" not in STATUS_COLORS


def test_status_word_never_truncated_only_label_is():
    line = format_quest_line(
        **_row(status="merge_blocked", step="x"),
        step_label="resolving deferred items", color=False)
    assert "merge blocked" in line               # status word intact
    assert "…" in line                           # label was truncated
    assert "resolving deferred items" not in line  # full label did not fit


def test_merge_blocked_status_word_is_wordmark_under_dark_theme() -> None:
    from imp.cli import palette as pal
    try:
        pal.set_theme("dark")
        line = format_quest_line(**_row(status="merge_blocked", step="x"), color=True)
    finally:
        pal.set_theme("ansi")
    wordmark = pal.theme_escapes("dark")["wordmark"]   # \x1b[38;2;255;154;60m
    yellow = pal.theme_escapes("dark")["yellow"]        # \x1b[38;2;199;151;106m
    assert wordmark + "merge blocked" in line           # status word painted wordmark
    assert yellow not in line                           # not the muted yellow role


def test_intent_fallback_for_empty_title():
    line = format_quest_line(**_row(title="   ", status="running", step="s"),
                              intent="add a dark mode toggle", color=False)
    assert "add a dark mode toggle" in line
    assert "(unnamed)" not in line


def test_intent_fallback_for_empty_title_color():
    line = format_quest_line(**_row(title="   ", status="running", step="s"),
                              intent="add a dark mode toggle", color=True)
    # the dimmed opening-prompt fallback now carries the `> ` prompt prefix
    assert "\x1b[2m> add a dark mode toggle\x1b[0m" in line
    assert "(unnamed)" not in line


def test_intent_fallback_flattens_multiline_prompt():
    # A multi-line opening prompt (e.g. a pasted traceback) must collapse to a
    # single row; an embedded newline in the NAME cell would spill the table.
    line = format_quest_line(
        **_row(title="", status="running", step="s"),
        intent="Ready to land the draft?\n  + Exception Group Traceback\n  |  File",
        color=False, name_width=80)
    assert "\n" not in line
    assert "Ready to land the draft? + Exception Group Traceback | File" in line


def test_unnamed_when_no_intent():
    line = format_quest_line(**_row(title="   ", status="running", step="s"),
                              intent=None, color=False)
    assert "(unnamed)" in line


def test_status_cell_includes_phase_label():
    line = format_quest_line(**_row(status="running", step="write_spec"),
                              step_label="writing spec",
                              phase_label="spec phase", color=False)
    assert "running (spec phase · writing spec)" in line


def test_status_cell_omits_phase_when_none():
    line = format_quest_line(**_row(status="running", step="write_spec"),
                              step_label="writing spec",
                              phase_label=None, color=False)
    assert "running (writing spec)" in line
    assert "·" not in line


def test_status_width_narrows_label_budget():
    # status_width 20 -> budget 20-7-3 = 10, so a 30-char label is clipped.
    line = format_quest_line(**_row(status="running", step="x"),
                              step_label="a" * 30, status_width=20, color=False)
    assert "…" in line
    assert "a" * 30 not in line


def test_status_subfloor_drops_suffix_no_overflow():
    # status_width 8 -> budget 8-7-3 = -2 < 1, so the suffix is dropped and the
    # bare status word is rendered (never the untruncated label).
    line = format_quest_line(**_row(status="running", step="x"),
                              step_label="a very long label that will not fit",
                              status_width=8, color=False)
    assert "running" in line
    assert "(" not in line            # suffix dropped entirely
    assert "a very long label" not in line


def test_header_line_honors_status_width():
    from imp.cli.quest_row import ID_W
    h = header_line(color=False, name_width=30, status_width=25)
    # UPDATED starts after ID + gap + NAME(30) + gap + STATUS(25) + gap.
    assert h.index("UPDATED") == ID_W + 2 + 30 + 2 + 25 + 2


def test_status_colors_are_meaning_based() -> None:
    from imp.cli.quest_row import STATUS_COLORS
    assert STATUS_COLORS["running"] == ("bold",)
    assert STATUS_COLORS["waiting"] == ("wordmark",)
    assert STATUS_COLORS["error"] == ("red",)
    assert STATUS_COLORS["merge_blocked"] == ("wordmark",)
    assert "new" not in STATUS_COLORS       # plain, at rest
    assert "paused" not in STATUS_COLORS
    assert "merged" not in STATUS_COLORS
    assert "backlog" not in STATUS_COLORS
    assert "draft" not in STATUS_COLORS


@pytest.mark.parametrize(
    "iso, expected",
    [
        ("2026-06-20T11:00:00.123456+00:00", "2026-06-20 11:00:00"),  # micros + offset dropped
        ("2026-06-20T11:00:00+00:00", "2026-06-20 11:00:00"),         # already second-precision
        (None, "-"),
        ("", "-"),
        ("not-a-date", "-"),
    ],
)
def test_format_timestamp_trims_to_seconds(iso, expected):
    from imp.cli.quest_row import format_timestamp
    assert format_timestamp(iso) == expected


def test_status_cell_phase_label_color_path():
    line = format_quest_line(**_row(status="running", step="write_spec"),
                              step_label="writing spec",
                              phase_label="spec phase", color=True)
    assert "\x1b[1mrunning\x1b[0m" in line
    assert "\x1b[2m (spec phase · writing spec)\x1b[0m" in line


def test_compose_status_label_variants():
    from imp.cli.quest_row import compose_status_label
    assert compose_status_label("-", None, None) is None
    assert compose_status_label("", "x", "p") is None
    assert compose_status_label("write", "writing", None) == "writing"
    assert compose_status_label("write", None, None) == "write"
    assert compose_status_label("write", "writing", "spec phase") == "spec phase · writing"


def _term(monkeypatch, cols):
    import os
    monkeypatch.setattr("shutil.get_terminal_size",
                        lambda fallback=(80, 24): os.terminal_size((cols, 24)))


def test_overview_widths_80col_name_full_status_clips(monkeypatch):
    from imp.cli.quest_row import overview_widths
    _term(monkeypatch, 80)
    # region = 80 - (6 + 7 + 6) = 61; name_content = 20; status_content = 7+40+3 = 50.
    status_w, name_w = overview_widths([("running", "x" * 40, "y" * 20)])
    assert (status_w, name_w) == (41, 20)


def test_overview_widths_wide_terminal_shows_full_status(monkeypatch):
    from imp.cli.quest_row import overview_widths
    _term(monkeypatch, 130)
    # region = 111; status_content = 50 fits, so STATUS gets its content width.
    status_w, name_w = overview_widths([("running", "x" * 40, "y" * 20)])
    assert (status_w, name_w) == (50, 61)


def test_overview_widths_name_cap_protects_status(monkeypatch):
    from imp.cli.quest_row import overview_widths
    _term(monkeypatch, 130)
    # An 80-char name is capped at 60 for sizing, so STATUS still gets its 50.
    status_w, _name_w = overview_widths([("running", "x" * 40, "y" * 80)])
    assert status_w == 50


def test_overview_widths_subfloor_floors_status_to_word(monkeypatch):
    from imp.cli.quest_row import overview_widths
    _term(monkeypatch, 80)
    # region = 61; a long intent hits the 60 cap so STATUS floors to the word (7).
    status_w, name_w = overview_widths([("running", "x" * 40, "i" * 80)])
    assert (status_w, name_w) == (7, 54)


def test_overview_widths_floors_name_at_min(monkeypatch):
    from imp.cli.quest_row import NAME_MIN, overview_widths
    _term(monkeypatch, 20)
    # region = 1; STATUS keeps its 6-wide header floor, NAME never drops below NAME_MIN.
    status_w, name_w = overview_widths([("new", None, "a")])
    assert name_w == NAME_MIN
    assert status_w == 6


def test_overview_widths_empty_rows(monkeypatch):
    from imp.cli.quest_row import overview_widths
    _term(monkeypatch, 80)
    # region = 61; no rows -> status_floor=status_content=6, name_content=0.
    assert overview_widths([]) == (6, 55)


def test_intent_fallback_carries_prompt_prefix():
    line = format_quest_line(**_row(title="   ", status="running", step="s"),
                              intent="add a dark mode toggle", color=False)
    assert "> add a dark mode toggle" in line
    assert "(unnamed)" not in line


def test_unnamed_has_no_prompt_prefix():
    line = format_quest_line(**_row(title="   ", status="running", step="s"),
                              intent=None, color=False)
    assert "(unnamed)" in line
    assert "> (unnamed)" not in line


def test_intent_fallback_prefix_counts_against_name_width():
    # name_width 24: cell = "> " (2) + 21 prompt chars + "…" = 24 display cols.
    line = format_quest_line(**_row(title="", status="running", step="s"),
                              intent="y" * 40, color=False, name_width=24)
    assert "> " + "y" * 21 + "…" in line
    assert "y" * 22 not in line


def test_resolve_display_name_title_kind_and_text():
    from imp.cli.quest_row import NameKind, resolve_display_name
    r = resolve_display_name("Add version flag", None)
    assert r.text == "Add version flag"
    assert r.kind is NameKind.TITLE


def test_resolve_display_name_strips_title():
    from imp.cli.quest_row import NameKind, resolve_display_name
    r = resolve_display_name("  Add version flag  ", None)
    assert r.text == "Add version flag"
    assert r.kind is NameKind.TITLE


def test_resolve_display_name_prompt_flattens_whitespace():
    from imp.cli.quest_row import NameKind, resolve_display_name
    r = resolve_display_name("   ", "add a\n  dark   mode\ttoggle")
    assert r.text == "add a dark mode toggle"
    assert r.kind is NameKind.PROMPT


def test_resolve_display_name_prompt_is_not_truncated():
    # The resolver holds no width; truncation is the caller's job.
    from imp.cli.quest_row import NameKind, resolve_display_name
    r = resolve_display_name("", "y" * 300)
    assert r.text == "y" * 300
    assert r.kind is NameKind.PROMPT


def test_resolve_display_name_unnamed_when_no_intent():
    from imp.cli.quest_row import NameKind, resolve_display_name
    r = resolve_display_name("", None)
    assert r.text == "(unnamed)"
    assert r.kind is NameKind.UNNAMED


def test_resolve_display_name_unnamed_when_intent_is_blank():
    # A whitespace-only prompt flattens to empty, so it lands on (unnamed).
    from imp.cli.quest_row import NameKind, resolve_display_name
    r = resolve_display_name(None, "   \n  \t ")
    assert r.text == "(unnamed)"
    assert r.kind is NameKind.UNNAMED


def test_resolve_display_name_title_wins_over_intent():
    from imp.cli.quest_row import NameKind, resolve_display_name
    r = resolve_display_name("Real title", "some intent")
    assert r.text == "Real title"
    assert r.kind is NameKind.TITLE


def test_name_column_right_padded_so_status_aligns():
    from imp.cli.quest_row import ID_W
    # Short title, fixed name_width -> STATUS starts at a deterministic offset:
    # ID_W + 2 (gap) + name_width + 2 (gap).
    line = format_quest_line(**_row(title="abc"), color=False, name_width=20)
    assert line.index("running") == ID_W + 2 + 20 + 2


def test_updated_is_final_column_no_trailing_whitespace():
    line = format_quest_line(**_row(), color=False)
    assert line == line.rstrip()          # UPDATED trails with no padding
    assert line.rstrip().endswith("15m")  # the relative age is the last cell


def test_overview_widths_name_cap_clamps_name_width(monkeypatch):
    from imp.cli.quest_row import overview_widths
    _term(monkeypatch, 200)
    # region = 200 - 19 = 181; uncapped NAME would be 131. name_cap clamps it to 60.
    status_w, name_w = overview_widths([("running", "x" * 40, "y" * 80)], name_cap=60)
    assert name_w == 60
    assert status_w == 50          # STATUS sizing is untouched by the cap


def test_overview_widths_name_cap_none_matches_uncapped(monkeypatch):
    from imp.cli.quest_row import overview_widths
    _term(monkeypatch, 200)
    rows = [("running", "x" * 40, "y" * 80)]
    assert overview_widths(rows, name_cap=None) == overview_widths(rows)
    _status_w, name_w = overview_widths(rows)
    assert name_w == 131           # uncapped, byte-for-byte today's behavior


def test_overview_widths_name_cap_inert_when_name_already_narrow(monkeypatch):
    from imp.cli.quest_row import overview_widths
    _term(monkeypatch, 80)
    # region = 61; NAME is already 20 (< 60), so name_cap=60 is inert.
    rows = [("running", "x" * 40, "y" * 20)]
    assert overview_widths(rows, name_cap=60) == overview_widths(rows)

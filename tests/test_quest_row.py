import re
from datetime import UTC, datetime

import pytest

from imp.cli.quest_row import (
    fallback_intent,
    format_quest_line,
    header_line,
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
                last_iso="2026-06-11T11:45:00+00:00", now=_NOW, status_width=35)
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
    h = header_line(color=False, name_width=40, status_width=35)
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
    assert "\x1b[1mrunning\x1b[0m" not in line   # no longer bold
    assert "running" in line                       # renders plain


def test_waiting_status_renders_bold():
    line = format_quest_line(**_row(status="waiting"), color=True)
    assert "\x1b[1mwaiting\x1b[0m" in line


def test_error_status_renders_bold():
    line = format_quest_line(**_row(status="error"), color=True)
    assert "\x1b[1merror\x1b[0m" in line


def test_merge_blocked_renders_two_words_bold():
    line = format_quest_line(**_row(status="merge_blocked"), color=True)
    assert "\x1b[1mmerge blocked\x1b[0m" in line   # bold weight, two-word text
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


def test_completed_status_renders_plain():
    line = format_quest_line(**_row(status="completed"), color=True)
    assert "completed" in line
    assert not re.search(r"\x1b\[[\d;]*m\s*completed", line)


def test_header_column_reads_updated_not_age():
    h = header_line(color=False, name_width=40, status_width=35)
    assert "UPDATED" in h
    assert "AGE" not in h


def test_status_cell_merges_step_label_as_dim_suffix():
    line = format_quest_line(**_row(status="running", step="write_spec"),
                              step_label="writing spec", color=True)
    # status word renders plain; the label suffix is still dim and parenthesized.
    assert "\x1b[1mrunning\x1b[0m" not in line
    assert "running" in line
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


def test_oversized_label_truncated_with_ellipsis():
    # status_width 35 -> room 35-13-3 = 19; the 24-char label truncates to 18 chars + "…".
    line = format_quest_line(
        **_row(status="merge_blocked", step="x"),
        step_label="resolving deferred items", color=False)
    assert "merge blocked (resolving deferred…)" in line
    assert "resolving deferred items" not in line


def test_merge_blocked_status_word_is_bold_theme_independent() -> None:
    from imp.cli import palette as pal
    try:
        pal.set_theme("dark")
        line = format_quest_line(**_row(status="merge_blocked", step="x"), color=True)
    finally:
        pal.set_theme("ansi")
    wordmark = pal.theme_escapes("dark")["wordmark"]   # \x1b[38;2;255;154;60m
    assert "\x1b[1mmerge blocked" in line               # bold weight, not hued
    assert wordmark not in line                         # no warm-gold hue survives


def test_intent_fallback_for_empty_title():
    line = format_quest_line(**_row(title="   ", status="running", step="s"),
                              intent="add a dark mode toggle", color=False)
    assert "add a dark mode toggle" in line
    assert "(unnamed)" not in line


def test_intent_fallback_for_empty_title_color():
    line = format_quest_line(**_row(title="   ", status="running", step="s"),
                              intent="add a dark mode toggle", color=True)
    # the opening-message fallback is dim and carries no prefix
    assert "\x1b[2madd a dark mode toggle\x1b[0m" in line
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


def test_status_width_truncates_oversized_label():
    # status_width 20 -> room 20-7-3 = 10; a 30-char label truncates to 9 chars + "…".
    line = format_quest_line(**_row(status="running", step="x", status_width=20),
                              step_label="a" * 30, color=False)
    assert "running (" + "a" * 9 + "…)" in line
    assert "a" * 10 not in line


def test_status_subfloor_drops_suffix_no_overflow():
    # status_width 8 -> budget 8-7-3 = -2 < 1, so the suffix is dropped and the
    # bare status word is rendered (never the untruncated label).
    line = format_quest_line(**_row(status="running", step="x", status_width=8),
                              step_label="a very long label that will not fit",
                              color=False)
    assert "running" in line
    assert "(" not in line            # suffix dropped entirely
    assert "a very long label" not in line


def test_status_label_fits_at_budget_boundary():
    # status_width 20 -> budget 10; a label of exactly 10 chars still fits in full.
    line = format_quest_line(**_row(status="running", step="x", status_width=20),
                              step_label="b" * 10, color=False)
    assert "running (" + "b" * 10 + ")" in line


def test_status_label_truncates_one_char_over_budget():
    # status_width 20 -> room 10; an 11-char label is one over and truncates to 9 + "…".
    line = format_quest_line(**_row(status="running", step="x", status_width=20),
                              step_label="b" * 11, color=False)
    assert "running (" + "b" * 9 + "…)" in line


def test_header_line_honors_status_width():
    from imp.cli.quest_row import ID_W
    h = header_line(color=False, name_width=30, status_width=25)
    # UPDATED starts after ID + gap + NAME(30) + gap + STATUS(25) + gap.
    assert h.index("UPDATED") == ID_W + 2 + 30 + 2 + 25 + 2


def test_status_colors_are_meaning_based() -> None:
    from imp.cli.quest_row import STATUS_COLORS
    assert "running" not in STATUS_COLORS    # plain, no longer bold
    assert STATUS_COLORS["waiting"] == ("bold",)
    assert STATUS_COLORS["error"] == ("bold",)
    assert STATUS_COLORS["merge_blocked"] == ("bold",)
    assert "new" not in STATUS_COLORS       # plain, at rest
    assert "paused" not in STATUS_COLORS
    assert "completed" not in STATUS_COLORS
    assert "backlog" not in STATUS_COLORS
    assert "draft" not in STATUS_COLORS


@pytest.mark.parametrize("iso", [None, "", "not-a-date"])
def test_format_timestamp_returns_dash_for_unparseable(iso):
    from imp.cli.quest_row import format_timestamp
    assert format_timestamp(iso) == "-"


def test_format_timestamp_renders_local_time():
    import os
    import time

    from imp.cli.quest_row import format_timestamp
    saved = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"   # June → EDT (UTC-4), no DST ambiguity
    time.tzset()
    try:
        # Stored value is UTC-aware; 11:00 UTC renders as 07:00 local, seconds
        # precision, no zone label. Micros + offset are dropped by strftime.
        assert format_timestamp(
            "2026-06-20T11:00:00.123456+00:00") == "2026-06-20 07:00:00"
        assert format_timestamp(
            "2026-06-20T11:00:00+00:00") == "2026-06-20 07:00:00"
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()


def test_status_cell_phase_label_color_path():
    line = format_quest_line(**_row(status="running", step="write_spec"),
                              step_label="writing spec",
                              phase_label="spec phase", color=True)
    assert "\x1b[1mrunning\x1b[0m" not in line
    assert "running" in line
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


def test_overview_widths_80col_truncates_label_that_does_not_fit(monkeypatch):
    from imp.cli.quest_row import overview_widths
    _term(monkeypatch, 80)
    # region = 61; NAME takes its 20, leaving 41 — the 50-col labelled cell truncates to 41.
    status_w, name_w = overview_widths([("running", "x" * 40, "y" * 20)])
    assert (status_w, name_w) == (41, 20)


def test_overview_widths_wide_terminal_shows_full_status(monkeypatch):
    from imp.cli.quest_row import overview_widths
    _term(monkeypatch, 130)
    # region = 111; the 50-col labelled cell fits, and NAME stays at its content.
    status_w, name_w = overview_widths([("running", "x" * 40, "y" * 20)])
    assert (status_w, name_w) == (50, 20)


def test_overview_widths_status_is_widest_rendered_cell(monkeypatch):
    from imp.cli.quest_row import overview_widths
    _term(monkeypatch, 100)
    # region = 81, NAME 20 -> 61 left: the 30-col cell fits, the 70-col one truncates
    # to the 61-col budget, so STATUS is 61.
    status_w, name_w = overview_widths([("running", "a" * 20, "n" * 20),
                                        ("waiting", "b" * 60, "n" * 20)])
    assert (status_w, name_w) == (61, 20)


def test_overview_widths_indent_shrinks_the_status_budget(monkeypatch):
    from imp.cli.quest_row import overview_widths
    _term(monkeypatch, 80)
    rows = [("running", "x" * 30, "y" * 20)]   # labelled cell = 40 cols
    assert overview_widths(rows) == (40, 20)            # 61 - 20 = 41 left: fits
    assert overview_widths(rows, indent=2) == (39, 20)  # 59 - 20 = 39 left: truncates


def test_overview_widths_name_cap_protects_status(monkeypatch):
    from imp.cli.quest_row import overview_widths
    _term(monkeypatch, 140)
    # region = 121; an 80-char name is capped at 70 for sizing, so STATUS still gets its 50.
    status_w, _name_w = overview_widths([("running", "x" * 40, "y" * 80)])
    assert status_w == 50


def test_overview_widths_capped_name_leaves_no_room_for_label(monkeypatch):
    from imp.cli.quest_row import overview_widths
    _term(monkeypatch, 80)
    # region = 61; NAME claims 54, leaving 7 — too few for the labelled cell, so the label drops.
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
    # No rows: STATUS keeps its header width, NAME its NAME_MIN floor.
    assert overview_widths([]) == (6, 10)


def test_intent_fallback_has_no_prompt_prefix():
    line = format_quest_line(**_row(title="   ", status="running", step="s"),
                              intent="add a dark mode toggle", color=False)
    assert line.startswith("yaitd7  add a dark mode toggle")   # NAME cell starts at the text
    assert "(unnamed)" not in line


def test_unnamed_has_no_prompt_prefix():
    line = format_quest_line(**_row(title="   ", status="running", step="s"),
                              intent=None, color=False)
    assert "(unnamed)" in line
    assert "> (unnamed)" not in line


def test_intent_fallback_clips_to_name_width():
    # name_width 24: cell = 23 prompt chars + "…" = 24 display cols.
    line = format_quest_line(**_row(title="", status="running", step="s"),
                              intent="y" * 40, color=False, name_width=24)
    assert "y" * 23 + "…" in line
    assert "y" * 24 not in line


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


def test_overview_widths_caps_name_at_name_cap(monkeypatch):
    from imp.cli.quest_row import NAME_CAP, overview_widths
    _term(monkeypatch, 200)
    # region = 200 - 19 = 181; STATUS takes its 50-col content and NAME stops at the cap.
    status_w, name_w = overview_widths([("running", "x" * 40, "y" * 80)])
    assert name_w == NAME_CAP == 70
    assert status_w == 50


def test_fallback_intent_prefers_the_events_intent():
    assert fallback_intent("fix the login crash", "seeded brief") == "fix the login crash"


def test_fallback_intent_falls_back_to_seed_opening_when_intent_absent():
    assert fallback_intent(None, "seeded brief") == "seeded brief"


def test_fallback_intent_treats_a_blank_intent_as_absent():
    assert fallback_intent("  \n\t ", "seeded brief") == "seeded brief"


def test_fallback_intent_is_none_when_both_are_blank_or_absent():
    assert fallback_intent(None, None) is None
    assert fallback_intent("", "   ") is None


def test_fallback_intent_returns_text_unflattened():
    # Flattening/clipping is the resolver's and caller's job, not the source picker's.
    assert fallback_intent(None, "line one\n  line two") == "line one\n  line two"


def test_overview_row_has_no_padding_gap(monkeypatch):
    from imp.cli.quest_row import overview_widths
    _term(monkeypatch, 80)
    status_w, name_w = overview_widths([("running", "l" * 60, "Fix the login button")])
    assert header_line(color=False, name_width=name_w, status_width=status_w) == (
        "ID      NAME                  STATUS" + " " * 37 + "UPDATED")
    line = format_quest_line(
        quest_id="abc123", title="Fix the login button", status="running", step="x",
        step_label="l" * 60, last_iso="2026-06-11T11:59:58+00:00", now=_NOW,
        color=False, name_width=name_w, status_width=status_w)
    assert line == "abc123  Fix the login button  running (" + "l" * 30 + "…)  2s"


def test_overview_clips_long_name_at_cap_on_wide_terminal(monkeypatch):
    from imp.cli.quest_row import overview_widths
    _term(monkeypatch, 200)
    status_w, name_w = overview_widths([("running", None, "y" * 80)])
    line = format_quest_line(**_row(title="y" * 80, step="-", name_width=name_w,
                                    status_width=status_w), color=False)
    assert "  " + "y" * 69 + "…  running  15m" in line


@pytest.mark.parametrize("cols", [200, 120, 80, 30])
def test_overview_header_status_aligns_with_row_status(monkeypatch, cols):
    from imp.cli.quest_row import ID_W, overview_widths
    _term(monkeypatch, cols)
    specs = [("running", "s" * 40, "y" * 80), ("waiting", None, "short")]
    status_w, name_w = overview_widths(specs)
    col = ID_W + 2 + name_w + 2
    assert header_line(color=False, name_width=name_w,
                       status_width=status_w)[col:].startswith("STATUS")
    for status, label, name in specs:
        line = format_quest_line(**_row(title=name, status=status,
                                        step="x" if label else "-",
                                        name_width=name_w, status_width=status_w),
                                 step_label=label, color=False)
        assert line[col:].startswith(status)
        assert "(…" not in line          # never an empty cut


_SPEC_LABEL = "spec phase · reviewing spec"


def test_fit_label_truncates_with_ellipsis():
    from imp.cli.quest_row import _fit_label
    cell = _fit_label("paused", _SPEC_LABEL, 31)
    assert cell == "paused (spec phase · reviewin…)"
    assert len(cell) == 31


def test_fit_label_strips_trailing_separators_before_ellipsis():
    from imp.cli.quest_row import _fit_label
    # width 23 -> room 14, cut label[:13] == "spec phase · " -> stripped to "spec phase".
    assert _fit_label("paused", _SPEC_LABEL, 23) == "paused (spec phase…)"
    # width 17 -> cut label[:7] == "review " -> stripped to "review".
    assert _fit_label("paused", "review spec phase", 17) == "paused (review…)"


def test_fit_label_bare_word_when_fewer_than_five_label_chars_fit():
    from imp.cli.quest_row import _fit_label
    assert _fit_label("paused", _SPEC_LABEL, 14) == "paused"   # room - 1 == 4
    # width 15 -> cut "spec " strips to "spec" (4 chars) -> bare word.
    assert _fit_label("paused", _SPEC_LABEL, 15) == "paused"
    assert _fit_label("running", "a very long label", 8) == "running"   # negative room


def test_fit_label_whole_label_and_none_unchanged():
    from imp.cli.quest_row import _fit_label
    assert _fit_label("running", "threshold", 35) == "running (threshold)"
    assert _fit_label("running", None, 35) == "running"


def test_overview_120col_truncates_status_under_capped_name(monkeypatch):
    from imp.cli.quest_row import ID_W, overview_widths
    _term(monkeypatch, 120)
    # region = 101; NAME capped at 70 leaves STATUS 31 — the 36-col cell truncates.
    status_w, name_w = overview_widths([("paused", _SPEC_LABEL, "y" * 80)])
    assert (status_w, name_w) == (31, 70)
    col = ID_W + 2 + name_w + 2
    assert header_line(color=False, name_width=name_w,
                       status_width=status_w)[col:].startswith("STATUS")
    line = format_quest_line(**_row(title="y" * 80, status="paused", step="review-spec",
                                    name_width=name_w, status_width=status_w),
                             step_label="reviewing spec", phase_label="spec phase",
                             color=False)
    assert line[col:].startswith("paused (spec phase · reviewin…)  15m")


def test_truncated_suffix_renders_dim_and_status_word_keeps_style():
    line = format_quest_line(**_row(status="waiting", step="review-spec", status_width=31),
                             step_label="reviewing spec", phase_label="spec phase",
                             color=True)
    assert "\x1b[1mwaiting\x1b[0m\x1b[2m (spec phase · reviewi…)\x1b[0m" in line

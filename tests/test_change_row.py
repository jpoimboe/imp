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
                status="running", step="implement",
                last_iso="2026-06-11T11:45:00+00:00", now=_NOW)
    base.update(kw)
    return base


def test_format_columns_plain():
    line = format_change_line(**_row(), color=False)
    assert "\x1b[" not in line
    assert line.startswith("yaitd7")        # ID first
    assert "Add version flag" in line       # NAME
    assert "running" in line
    assert "implement" in line
    assert "15m" in line                    # relative AGE


def test_name_is_final_column():
    line = format_change_line(**_row(), color=False)
    # NAME now trails every other column.
    assert line.index("Add version flag") > line.index("running")
    assert line.index("Add version flag") > line.index("implement")
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

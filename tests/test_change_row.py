from datetime import UTC, datetime

import pytest

from imp.cli.change_row import format_change_line, header_line, relative_age

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
    assert "Add version flag" in line       # TITLE
    assert "running" in line
    assert "implement" in line
    assert "15m" in line                    # relative AGE


def test_format_unnamed_shows_id_and_unnamed_label():
    line = format_change_line(**_row(title="   "), color=False)
    assert line.startswith("yaitd7")
    assert "(unnamed)" in line


def test_header_line_leads_with_id_then_title():
    h = header_line(color=False)
    assert h.startswith("ID")
    assert "TITLE" in h
    assert "HANDLE" not in h


def test_format_truncates_long_title():
    line = format_change_line(**_row(title="x" * 40), color=False)
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

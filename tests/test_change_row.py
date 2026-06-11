from datetime import UTC, datetime

import pytest

from imp.cli.change_row import format_change_line, header_line, relative_age, resume_token

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


@pytest.mark.parametrize(
    "change_id, expected",
    [
        ("2026-06-11-yaitd7", "yaitd7"),                 # modern id → 6-char suffix
        ("2026-06-11-add-version-flag",
         "2026-06-11-add-version-flag"),                 # legacy descriptive → full id
        ("legacy-no-date", "legacy-no-date"),            # no date prefix → full id
        ("2026-06-11-AB12CD", "2026-06-11-AB12CD"),      # uppercase tail → not modern → full id
    ],
)
def test_resume_token(change_id, expected):
    assert resume_token(change_id) == expected


def _row(**kw):
    base = dict(change_id="2026-06-11-yaitd7", title="Add version flag",
                status="running", step="implement",
                last_iso="2026-06-11T11:45:00+00:00", now=_NOW)
    base.update(kw)
    return base


def test_format_columns_plain():
    # color off → no SGR; fixed-width columns; relative age; resume-token ID.
    line = format_change_line(**_row(), color=False)
    assert "\x1b[" not in line
    assert line.startswith("Add version flag" + " " * 8)   # TITLE padded to 24
    assert "running" in line
    assert "implement" in line
    assert "15m" in line                                   # relative AGE
    assert line.rstrip().endswith("yaitd7")                # ID = resume token


def test_format_truncates_long_title():
    line = format_change_line(**_row(title="x" * 40), color=False)
    # Truncated to 24 display cols: 23 chars + ellipsis.
    assert "x" * 23 + "…" in line
    assert "x" * 24 not in line


def test_format_unnamed_title():
    line = format_change_line(**_row(title="   "), color=False)
    assert "(unnamed)" in line


def test_format_status_color_running():
    # color on → only the STATUS value is wrapped in its color (ansi theme green).
    line = format_change_line(**_row(status="running"), color=True)
    assert "\x1b[32mrunning\x1b[0m" in line


def test_format_dim_wraps_whole_row():
    # The --all past section: whole row dimmed, no per-status color.
    line = format_change_line(**_row(status="completed"), color=True, dim=True)
    assert line.startswith("\x1b[2m")
    assert line.endswith("\x1b[0m")
    assert "\x1b[32m" not in line                          # no per-status color in dim mode


def test_header_line_columns_plain():
    h = header_line(color=False)
    assert h.startswith("TITLE" + " " * 19)                # TITLE header padded to 24
    assert h.index("STATUS") == 26                         # 24 + 2-space gap
    assert h.rstrip().endswith("ID")

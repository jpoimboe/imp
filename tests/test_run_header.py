from __future__ import annotations

from imp.cli.main import _run_header_line


def test_named_change_header_is_slug_plus_marker_plain() -> None:
    # No leading '#', no title duplication: slug only + dim marker.
    line = _run_header_line(
        "2026-06-11-add-version-flag", "2026-06-11-yaitd7", "new", color=False)
    assert line == "2026-06-11-add-version-flag · new"


def test_titleless_change_falls_back_to_the_dated_id() -> None:
    # Empty slug (unnamed Change) → the <date>-<suffix> id, never nothing.
    line = _run_header_line("", "2026-06-11-yaitd7", "resuming", color=False)
    assert line == "2026-06-11-yaitd7 · resuming"


def test_blank_slug_whitespace_also_falls_back() -> None:
    line = _run_header_line("   ", "2026-06-11-yaitd7", "new", color=False)
    assert line == "2026-06-11-yaitd7 · new"


def test_color_wraps_slug_bold_magenta_and_marker_dim_no_hash() -> None:
    line = _run_header_line(
        "2026-06-11-add-version-flag", "2026-06-11-yaitd7", "resuming", color=True)
    assert "#" not in line
    # slug in the workflow bold+magenta treatment; the marker dim.
    assert line.startswith("\x1b[1m\x1b[35m2026-06-11-add-version-flag\x1b[0m")
    assert "\x1b[2m · resuming\x1b[0m" in line

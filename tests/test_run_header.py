from __future__ import annotations

from imp.cli.main import _run_header_line


def test_named_change_leads_with_handle_then_title_and_marker() -> None:
    line = _run_header_line(
        "add-version-flag-yaitd7", "Add version flag", "yaitd7", "new", color=False)
    assert line == "add-version-flag-yaitd7 · Add version flag · new"


def test_unnamed_change_shows_bare_id_and_unnamed_label() -> None:
    line = _run_header_line("yaitd7", "", "yaitd7", "resuming", color=False)
    assert line == "yaitd7 · (unnamed) · resuming"


def test_color_wraps_head_bold_magenta_and_tail_dim() -> None:
    line = _run_header_line(
        "add-version-flag-yaitd7", "Add version flag", "yaitd7", "new", color=True)
    assert line == (
        "\x1b[1m\x1b[35madd-version-flag-yaitd7\x1b[0m"
        "\x1b[2m · Add version flag · new\x1b[0m")


def test_color_unnamed_shows_id_bold_magenta_and_dim_unnamed_tail() -> None:
    line = _run_header_line("yaitd7", "", "yaitd7", "resuming", color=True)
    assert line == (
        "\x1b[1m\x1b[35myaitd7\x1b[0m"
        "\x1b[2m · (unnamed) · resuming\x1b[0m")

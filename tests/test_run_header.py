from __future__ import annotations

from imp.cli.main import _run_header_line


def test_named_change_header_leads_with_id_and_quotes_title_plain() -> None:
    line = _run_header_line(
        "2026-06-11-yaitd7", "Add version flag", "new", color=False)
    assert line == "2026-06-11-yaitd7: 'Add version flag' · new"


def test_titleless_change_shows_only_the_id_plain() -> None:
    line = _run_header_line("2026-06-11-yaitd7", "", "resuming", color=False)
    assert line == "2026-06-11-yaitd7 · resuming"


def test_whitespace_title_is_treated_as_titleless() -> None:
    line = _run_header_line("2026-06-11-yaitd7", "   ", "new", color=False)
    assert line == "2026-06-11-yaitd7 · new"


def test_color_wraps_id_bold_magenta_and_label_dim() -> None:
    line = _run_header_line(
        "2026-06-11-yaitd7", "Add version flag", "resuming", color=True)
    assert line == (
        "\x1b[1m\x1b[35m2026-06-11-yaitd7\x1b[0m"
        "\x1b[2m: 'Add version flag' · resuming\x1b[0m")


def test_color_titleless_shows_id_bold_magenta_and_bare_dim_marker() -> None:
    line = _run_header_line("2026-06-11-yaitd7", "", "resuming", color=True)
    assert line == (
        "\x1b[1m\x1b[35m2026-06-11-yaitd7\x1b[0m"
        "\x1b[2m · resuming\x1b[0m")

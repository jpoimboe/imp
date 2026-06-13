from __future__ import annotations

from imp.cli.main import _run_header_line


def test_named_change_header_leads_with_token_and_slug_plain() -> None:
    line = _run_header_line(
        "2026-06-13-add-version-flag", "2026-06-11-yaitd7", "new", color=False)
    assert line == "yaitd7 · add-version-flag · new"


def test_titleless_change_shows_only_token_and_marker_plain() -> None:
    line = _run_header_line("", "2026-06-11-yaitd7", "resuming", color=False)
    assert line == "yaitd7 · resuming"


def test_legacy_id_token_is_the_full_id() -> None:
    line = _run_header_line("legacy-slug", "legacy-slug", "new", color=False)
    assert line == "legacy-slug · legacy-slug · new"


def test_color_wraps_token_bold_magenta_and_tail_dim() -> None:
    line = _run_header_line(
        "2026-06-13-add-version-flag", "2026-06-11-yaitd7", "new", color=True)
    assert line == (
        "\x1b[1m\x1b[35myaitd7\x1b[0m"
        "\x1b[2m · add-version-flag · new\x1b[0m")


def test_color_titleless_shows_token_bold_magenta_and_bare_dim_marker() -> None:
    line = _run_header_line("", "2026-06-11-yaitd7", "resuming", color=True)
    assert line == (
        "\x1b[1m\x1b[35myaitd7\x1b[0m"
        "\x1b[2m · resuming\x1b[0m")

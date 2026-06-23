from __future__ import annotations

from imp.cli.main import _print_run_header, _run_header_line


def test_named_change_leads_with_id_then_title_and_marker() -> None:
    line = _run_header_line("yaitd7", "Add version flag", "new", color=False)
    assert line == "yaitd7 · Add version flag · new"


def test_unnamed_change_shows_id_and_marker_only() -> None:
    line = _run_header_line("yaitd7", "", "resuming", color=False)
    assert line == "yaitd7 · resuming"


def test_whitespace_only_title_shows_id_and_marker_only() -> None:
    line = _run_header_line("yaitd7", "   ", "new", color=False)
    assert line == "yaitd7 · new"


def test_color_wraps_head_bold_yellow_and_tail_dim() -> None:
    line = _run_header_line("yaitd7", "Add version flag", "new", color=True)
    assert line == (
        "\x1b[1m\x1b[33myaitd7\x1b[0m"
        "\x1b[2m · Add version flag · new\x1b[0m")


def test_color_unnamed_shows_id_bold_yellow_and_dim_marker_tail() -> None:
    line = _run_header_line("yaitd7", "", "resuming", color=True)
    assert line == (
        "\x1b[1m\x1b[33myaitd7\x1b[0m"
        "\x1b[2m · resuming\x1b[0m")


def test_print_run_header_emits_marker_then_one_blank_line(capsys) -> None:
    _print_run_header("b1f6wi", "", "chat", color=False)
    assert capsys.readouterr().out == "b1f6wi · chat\n\n"


def test_print_run_header_keeps_title_when_named(capsys) -> None:
    _print_run_header("b1f6wi", "Dark mode", "new", color=False)
    assert capsys.readouterr().out == "b1f6wi · Dark mode · new\n\n"

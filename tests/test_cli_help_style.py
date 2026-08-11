from __future__ import annotations

import argparse
import re
import sys
from typing import Any

import pytest

import imp.cli.palette as pal
from imp.cli.help_format import (
    TAGLINE,
    ImpHelpFormatter,
    ImpParser,
    ImpRawHelpFormatter,
    ImpTopLevelHelpFormatter,
)
from imp.cli.main import _build_events_search_parser, _build_parser

DIM = "\x1b[2m"
RESET = "\x1b[0m"
GOLD = "\x1b[38;2;255;154;60m"          # palette's `wordmark` under the dark theme


def _escapes(text: str) -> set[str]:
    return set(re.findall(r"\x1b\[[0-9;]*m", text))


def _demo(**kwargs: Any) -> ImpParser:
    parser = ImpParser(prog="demo", **kwargs)
    parser.add_argument("--flag", help="A flag.")
    return parser


def test_chrome_is_dim_and_content_is_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "80")
    text = _demo(description="Plain prose about imp.").format_help()
    assert text.startswith(f"{DIM}usage:{RESET} demo")
    assert f"{DIM}options:{RESET}\n" in text
    assert "Plain prose about imp.\n" in text          # description unpainted
    assert "  --flag FLAG  A flag.\n" in text          # flags and their help unpainted
    assert _escapes(text) == {DIM, RESET}


def test_stock_argparse_color_is_switched_off() -> None:
    # argparse 3.14 defaults color=True; imp paints its own instead.
    assert ImpParser(prog="demo").color is False


def test_no_color_leaves_help_byte_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("NO_COLOR", "1")
    assert "\x1b" not in _demo(description=TAGLINE,
                               formatter_class=ImpTopLevelHelpFormatter).format_help()


def test_help_gains_one_trailing_blank_line_only_on_a_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    parser = _demo()
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    tty = parser.format_help()
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    piped = parser.format_help()
    assert tty == piped + "\n"
    assert tty.endswith("\n\n") and not tty.endswith("\n\n\n")
    assert piped.endswith("\n") and not piped.endswith("\n\n")


def test_help_spacing_survives_a_stdout_without_isatty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # add_subparsers() renders format_help() at construction time to compute the
    # subparsers' prog, so building a parser must not require a full stdout.
    class Minimal:
        def flush(self) -> None: ...
        def fileno(self) -> int:
            return 1

    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(sys, "stdout", Minimal())
    parser = ImpParser(prog="demo")
    parser.add_subparsers(dest="cmd", metavar="<command>")
    assert parser.format_help().endswith("\n")
    assert not parser.format_help().endswith("\n\n")


def test_usage_alone_never_gains_the_blank_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # argparse's error() prints format_usage() to stderr and then the message;
    # the help spacing rule must not wedge a blank line in between.
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    usage = _demo().format_usage()
    assert usage.endswith("[--flag FLAG]\n")
    assert not usage.endswith("\n\n")


def test_wordmark_is_scoped_to_the_top_level_formatter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "80")
    pal.set_theme("dark")
    top = _demo(description=TAGLINE,
                formatter_class=ImpTopLevelHelpFormatter).format_help()
    assert top.count(GOLD) == 1
    assert f"{GOLD}imp{RESET} — multi-agent software development workflow\n" in top
    assert _escapes(top) == {DIM, RESET, GOLD}
    # Same description, ordinary variant: no wordmark anywhere.
    assert GOLD not in _demo(description=TAGLINE).format_help()


def test_raw_variant_keeps_line_breaks_and_the_default_variant_fills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "100")
    body = "one two three\nfour five six"
    raw = _demo(description=body, formatter_class=ImpRawHelpFormatter).format_help()
    auto = _demo(description=body).format_help()
    assert "one two three\nfour five six\n" in raw
    assert "one two three four five six\n" in auto


def test_subparsers_inherit_imp_styling() -> None:
    parser = ImpParser(prog="demo")
    sub = parser.add_subparsers(dest="cmd")
    child = sub.add_parser("alpha", help="Alpha.")
    grandchild = child.add_subparsers(dest="act").add_parser("beta", help="Beta.")
    for p in (child, grandchild):
        assert isinstance(p, ImpParser)
        assert p.formatter_class is ImpHelpFormatter
        assert p.color is False


def _sub(name: str) -> argparse.ArgumentParser:
    parser = _build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices[name]
    raise AssertionError("no subparsers")


def test_show_help_wears_imp_chrome_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "80")
    pal.set_theme("dark")
    text = _sub("show").format_help()
    assert text.startswith(f"{DIM}usage:{RESET} imp show")
    assert f"{DIM}positional arguments:{RESET}\n" in text
    assert f"{DIM}options:{RESET}\n" in text
    assert _escapes(text) == {DIM, RESET}


def test_show_help_is_byte_clean_under_no_color(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    assert "\x1b" not in _sub("show").format_help()


def test_show_help_trailing_blank_follows_the_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["show", "-h"])
    tty = capsys.readouterr().out
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["show", "-h"])
    piped = capsys.readouterr().out
    assert tty == piped + "\n"
    assert tty.endswith("\n\n") and not tty.endswith("\n\n\n")
    assert piped.endswith("\n") and not piped.endswith("\n\n")


def test_init_help_fills_the_terminal_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "100")
    text = _sub("init").format_help()
    description = [line for line in text.split("\n")
                   if line and not line.startswith((" ", "usage"))
                   and not line.endswith(":")]
    assert description, text
    assert max(len(line) for line in description) > 70
    assert ("Makes an initial commit of imp/config.yaml and .gitignore."
            in " ".join(text.split()))


def test_events_help_keeps_its_hand_wrapped_line_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "100")
    text = _sub("events").format_help()
    assert ("Index and filter a quest's event log. Bare (imp events <id>): a\n"
            "steps+types index.") in text
    assert ("subcommands:\n"
            "  imp events search <query>\n"
            "      Substring search across event content (tool calls, results,\n"
            "      reasoning). Narrow with --type / --agent; full flags with:\n"
            "      imp events search -h\n") in text


def test_events_search_help_wears_imp_chrome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "80")
    pal.set_theme("dark")
    text = _build_events_search_parser().format_help()
    assert text.startswith(f"{DIM}usage:{RESET} imp events search")
    assert f"{DIM}options:{RESET}\n" in text
    assert _escapes(text) == {DIM, RESET}


def test_events_search_help_trailing_blank_follows_the_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    parser = _build_events_search_parser()
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    tty = parser.format_help()
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    piped = parser.format_help()
    assert tty == piped + "\n"
    assert piped.endswith("\n") and not piped.endswith("\n\n")


def test_version_gains_the_conventional_trailing_blank_on_a_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    # --version routes through format_help(), so it obeys the same tty blank rule — deliberate.
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["--version"])
    tty = capsys.readouterr().out
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["--version"])
    piped = capsys.readouterr().out
    assert piped.startswith("imp ")
    assert piped.endswith("\n") and not piped.endswith("\n\n")
    assert tty == piped + "\n"

import argparse
from types import SimpleNamespace

from imp.cli.main import _select_headless


def _stream(*, tty: bool) -> SimpleNamespace:
    return SimpleNamespace(isatty=lambda: tty)


def test_non_tty_without_flag_selects_headless_and_marks_auto() -> None:
    args = argparse.Namespace(non_interactive=False)
    headless, auto = _select_headless(
        args, stdin=_stream(tty=False), stdout=_stream(tty=False))
    assert headless is True and auto is True


def test_both_ttys_without_flag_stays_interactive() -> None:
    args = argparse.Namespace(non_interactive=False)
    headless, auto = _select_headless(
        args, stdin=_stream(tty=True), stdout=_stream(tty=True))
    assert headless is False and auto is False


def test_explicit_non_interactive_is_headless_but_not_auto() -> None:
    args = argparse.Namespace(non_interactive=True)
    headless, auto = _select_headless(
        args, stdin=_stream(tty=True), stdout=_stream(tty=True))
    assert headless is True and auto is False


def test_stdin_tty_stdout_non_tty_selects_headless_auto() -> None:
    args = argparse.Namespace(non_interactive=False)
    headless, auto = _select_headless(
        args, stdin=_stream(tty=True), stdout=_stream(tty=False))
    assert headless is True and auto is True

# tests/test_session_loop_predicate.py
from __future__ import annotations

from imp.cli.main import _interactive_loop_predicate


def test_predicate_all_conditions() -> None:
    # latched-armed AND both ttys AND interactive → True
    assert _interactive_loop_predicate(
        non_interactive=False, reader_armed=True,
        stdin_tty=True, stdout_tty=True) is True


def test_predicate_false_when_non_interactive() -> None:
    assert _interactive_loop_predicate(
        non_interactive=True, reader_armed=True,
        stdin_tty=True, stdout_tty=True) is False


def test_predicate_false_when_stdin_piped() -> None:
    # imp new < replies.txt: reader armed via /dev/tty, but stdin is a pipe.
    assert _interactive_loop_predicate(
        non_interactive=False, reader_armed=True,
        stdin_tty=False, stdout_tty=True) is False


def test_predicate_false_when_reader_never_armed() -> None:
    assert _interactive_loop_predicate(
        non_interactive=False, reader_armed=False,
        stdin_tty=True, stdout_tty=True) is False

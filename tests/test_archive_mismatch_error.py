"""ArchiveMismatch: the read-side downgrade guard's error type and its exact copy."""
from __future__ import annotations

from imp.engine.errors import ArchiveMismatch, LoadError

_DATA_LINE = (
    "archive data is at v8, newer than this imp (knows v7). A running imp keeps "
    "the code it started with, so this is usually a stale process, not a stale "
    "checkout. Fix: Ctrl-D and `imp resume` to restart this quest on the current "
    "install; if it persists, update imp. Agents: suggest this to the user."
)

_INDEX_LINE = (
    "archive index is at v6, newer than this imp (knows v5). A running imp keeps "
    "the code it started with, so this is usually a stale process, not a stale "
    "checkout. Fix: Ctrl-D and `imp resume` to restart this quest on the current "
    "install; if it persists, update imp. Agents: suggest this to the user."
)


def test_subclasses_loaderror() -> None:
    """main()'s existing `except (LoadError, LoadErrors)` is the only surfacing site."""
    assert isinstance(ArchiveMismatch(floor="data", stored=8, known=7), LoadError)


def test_data_floor_message_is_the_exact_line() -> None:
    assert str(ArchiveMismatch(floor="data", stored=8, known=7)) == _DATA_LINE


def test_index_floor_message_is_the_exact_line() -> None:
    assert str(ArchiveMismatch(floor="index", stored=6, known=5)) == _INDEX_LINE


def test_message_is_one_line_and_carries_the_floors() -> None:
    exc = ArchiveMismatch(floor="index", stored=6, known=5)
    # summarize_exception keeps only the first line; _pause_lines renders it as the head.
    assert len(str(exc).splitlines()) == 1
    assert (exc.floor, exc.stored, exc.known) == ("index", 6, 5)

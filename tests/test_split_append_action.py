"""SplitAppend: accumulate-and-comma-split argparse action for list options."""

from __future__ import annotations

import argparse

from imp.cli.main import SplitAppend


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--opt", action=SplitAppend, default=None)
    return p


def test_absent_option_is_none() -> None:
    assert _parser().parse_args([]).opt is None


def test_single_value() -> None:
    assert _parser().parse_args(["--opt", "a"]).opt == ["a"]


def test_equals_form() -> None:
    assert _parser().parse_args(["--opt=a"]).opt == ["a"]


def test_comma_joined_splits() -> None:
    assert _parser().parse_args(["--opt=a,b"]).opt == ["a", "b"]


def test_repeats_accumulate() -> None:
    assert _parser().parse_args(["--opt", "a", "--opt", "b"]).opt == ["a", "b"]


def test_comma_equals_repeat() -> None:
    comma = _parser().parse_args(["--opt=a,b"]).opt
    repeat = _parser().parse_args(["--opt", "a", "--opt", "b"]).opt
    assert comma == repeat == ["a", "b"]


def test_mixed_comma_and_repeat_concatenate() -> None:
    assert _parser().parse_args(["--opt=a,b", "--opt", "c"]).opt == ["a", "b", "c"]


def test_blanks_stripped_and_dropped() -> None:
    assert _parser().parse_args(["--opt= a , ,b, "]).opt == ["a", "b"]

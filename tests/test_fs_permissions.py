"""Filesystem permission matcher."""

from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.permissions import PermissionDenied, check_permission
from imp.engine.types import FsRule, SourceLoc


def _rule(ops: tuple[str, ...], paths: tuple[str, ...], mode: str) -> FsRule:
    return FsRule(
        operations=ops, paths=paths, mode=mode,  # type: ignore[arg-type]
        source=SourceLoc(file=Path("x"), line=1),
    )


def test_no_rules_allows_everything() -> None:
    check_permission((), "read", "/artifacts/spec.md")
    check_permission((), "write", "/scratch.txt")


def test_explicit_allow_passes() -> None:
    rules = (_rule(("read",), ("/project/**",), "allow"),)
    check_permission(rules, "read", "/project/src/main.py")


def test_explicit_deny_blocks() -> None:
    rules = (_rule(("write",), ("/**",), "deny"),)
    with pytest.raises(PermissionDenied):
        check_permission(rules, "write", "/anywhere.txt")


def test_first_match_wins() -> None:
    rules = (
        _rule(("write",), ("/scratch/**",), "allow"),
        _rule(("write",), ("/**",), "deny"),
    )
    check_permission(rules, "write", "/scratch/foo.txt")
    with pytest.raises(PermissionDenied):
        check_permission(rules, "write", "/other.txt")


def test_operation_specific() -> None:
    rules = (
        _rule(("read",), ("/project/**",), "allow"),
        _rule(("write",), ("/project/**",), "deny"),
    )
    check_permission(rules, "read", "/project/x.py")
    with pytest.raises(PermissionDenied):
        check_permission(rules, "write", "/project/x.py")


def test_single_star_matches_segment() -> None:
    rules = (
        _rule(("read",), ("/project/*.py",), "allow"),
        _rule(("read",), ("/**",), "deny"),
    )
    check_permission(rules, "read", "/project/main.py")
    with pytest.raises(PermissionDenied):
        check_permission(rules, "read", "/project/src/main.py")


def test_double_star_matches_recursively() -> None:
    rules = (_rule(("read",), ("/project/**",), "allow"),)
    check_permission(rules, "read", "/project/a/b/c.py")

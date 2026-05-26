# tests/test_permissions.py
from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.permissions import PermissionDenied, check_permission
from imp.engine.types import FsRule, SourceLoc


def _rule(ops: tuple[str, ...], paths: tuple[str, ...], mode: str) -> FsRule:
    return FsRule(
        operations=ops, paths=paths, mode=mode,  # type: ignore[arg-type]
        source=SourceLoc(file=Path("test.yaml"), line=1),
    )


def test_default_allow_read() -> None:
    # Empty rules + read → allow (default).
    check_permission([], "read", "/project/x.py")


def test_default_deny_write() -> None:
    with pytest.raises(PermissionDenied):
        check_permission([], "write", "/project/x.py")


def test_allow_write_then_default_deny() -> None:
    rules = [_rule(("write",), ("/artifacts/**",), "allow")]
    check_permission(rules, "write", "/artifacts/spec.md")
    with pytest.raises(PermissionDenied):
        check_permission(rules, "write", "/project/x.py")


def test_first_match_wins() -> None:
    rules = [
        _rule(("write",), ("/scratch/**",), "allow"),
        _rule(("write",), ("/**",), "deny"),
    ]
    check_permission(rules, "write", "/scratch/x")
    with pytest.raises(PermissionDenied):
        check_permission(rules, "write", "/project/x")


def test_glob_star_within_segment() -> None:
    rules = [_rule(("read",), ("/project/*.py",), "deny")]
    with pytest.raises(PermissionDenied):
        check_permission(rules, "read", "/project/main.py")
    # Subdir not matched by single-segment * — falls through to default-allow.
    check_permission(rules, "read", "/project/sub/main.py")


def test_doublestar_matches_subdirs() -> None:
    rules = [_rule(("write",), ("/project/**",), "deny")]
    with pytest.raises(PermissionDenied):
        check_permission(rules, "write", "/project/deep/nest/x.py")

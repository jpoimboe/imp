"""Builtin-tool filtering: undeclared tools raise a clear error if invoked."""

from __future__ import annotations

import pytest

from imp.agents.builtin_filter import (
    UndeclaredTool, build_filtered_builtin_overrides,
)


def test_undeclared_tool_returns_stub_that_raises() -> None:
    overrides = build_filtered_builtin_overrides(declared={"read_file"})
    by_name = {t.name: t for t in overrides}
    assert "execute" in by_name  # built-in not declared
    with pytest.raises(UndeclaredTool) as exc:
        by_name["execute"].invoke({"command": "ls"})
    assert "execute" in str(exc.value)
    assert "not declared" in str(exc.value).lower()


def test_declared_tool_is_not_in_overrides() -> None:
    """If an agent declares `execute`, we don't override it — deepagents'
    real execute should win."""
    overrides = build_filtered_builtin_overrides(declared={"execute"})
    names = {t.name for t in overrides}
    assert "execute" not in names
    assert "read_file" in names  # still filtered out


def test_complete_step_never_filtered() -> None:
    """complete_step doesn't exist in our world (response_format replaced it
    per Plan 2 review #5). Should never appear in overrides regardless."""
    overrides = build_filtered_builtin_overrides(declared=set())
    names = {t.name for t in overrides}
    assert "complete_step" not in names

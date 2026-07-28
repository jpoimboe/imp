from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.errors import LoadError
from imp.providers.effective import (
    memory_validation_scopes,
    validate_project_memory,
)
from tests.conftest import _seed_control


def test_scopes_enumerate_workflow_and_steps(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    valid, workflow_steps = memory_validation_scopes(
        tmp_path, ["imp/s.md", "all.md"]
    )
    # The seeded committed workflow dir is `imp`; its one step is named `s`.
    assert "imp" in valid
    assert workflow_steps == {"imp": frozenset({"s"})}


def test_scopes_only_include_referenced_workflows(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    valid, workflow_steps = memory_validation_scopes(tmp_path, ["all.md"])
    # A top-level relpath references no workflow subdir.
    assert workflow_steps == {}
    assert "imp" in valid


def test_validate_project_memory_still_raises_on_bad_tree(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    with pytest.raises(LoadError, match=r"invalid imp/memory/"):
        validate_project_memory(tmp_path, {"nope/all.md": "x"})


def test_validate_project_memory_accepts_valid_step_file(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    # `imp/s.md` targets the real step `s` of workflow `imp` — must not raise.
    assert validate_project_memory(tmp_path, {"imp/s.md": "x"}) is None


def test_validate_project_memory_empty_is_noop(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    # An empty snapshot returns without touching workflows.
    assert validate_project_memory(tmp_path, {}) is None


def test_preloaded_steps_parameter_removed(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    # The parameter is gone: passing it must be an unexpected-keyword TypeError,
    # not a silently-accepted no-op.
    with pytest.raises(TypeError, match="preloaded_steps"):
        memory_validation_scopes(
            tmp_path,
            ["imp/x.md"],
            preloaded_steps={"imp": frozenset({"planted"})},
        )


def test_scopes_still_accept_the_internal_memory_workflow(tmp_path: Path) -> None:
    valid, workflow_steps = memory_validation_scopes(
        tmp_path, ["memory/all.md"]
    )
    # Narrowing the *selectable* set must not narrow memory-tree validation.
    assert "memory" in valid
    assert "memory-editor" in workflow_steps["memory"]

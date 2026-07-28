"""Load-time validation of the imp/memory/ tree against the bundled workflows
(spec hede92 slice 2, AC 5): loud LoadError for a non-step .md, an unknown
workflow dir, and a stray top-level step file; the step error lists valid
steps. A valid tree and an empty tree pass silently."""
from __future__ import annotations

import pathlib

import pytest

from imp.engine.errors import LoadError
from imp.providers.effective import validate_project_memory


def test_empty_memory_is_noop(tmp_path: pathlib.Path) -> None:
    # No files -> nothing to validate, no workflow load, no raise.
    validate_project_memory(tmp_path, {})


def test_valid_tree_passes(tmp_path: pathlib.Path) -> None:
    # 'threshold' is a real step of the bundled 'default' workflow (resolved from
    # the bundled assets since tmp_path has no imp/workflows/ fork).
    validate_project_memory(
        tmp_path,
        {"all.md": "g", "default/all.md": "w", "default/threshold.md": "s"},
    )


def test_nonstep_file_raises_and_lists_steps(tmp_path: pathlib.Path) -> None:
    with pytest.raises(LoadError) as ei:
        validate_project_memory(tmp_path, {"default/notastep.md": "x"})
    msg = str(ei.value)
    assert "notastep" in msg
    assert "threshold" in msg  # a real default step is listed as valid


def test_unknown_workflow_dir_raises(tmp_path: pathlib.Path) -> None:
    with pytest.raises(LoadError) as ei:
        validate_project_memory(tmp_path, {"bogus/all.md": "x"})
    assert "bogus" in str(ei.value)


def test_stray_toplevel_step_file_raises(tmp_path: pathlib.Path) -> None:
    with pytest.raises(LoadError) as ei:
        validate_project_memory(tmp_path, {"threshold.md": "x"})
    assert "threshold.md" in str(ei.value)

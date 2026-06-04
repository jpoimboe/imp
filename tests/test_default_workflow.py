"""The shipped default workflow must pass loader validation as-is."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from imp.assets import default_workflow_dir
from imp.engine.errors import LoadErrors
from imp.engine.loader import load_workflow


def test_default_workflow_dir_exists() -> None:
    wf = default_workflow_dir()
    assert (wf / "workflow.yaml").is_file()
    assert (wf / "agents").is_dir()
    assert (wf / "scripts").is_dir()


def test_default_workflow_loads_with_default_roles(tmp_path: Path) -> None:
    # Stage the workflow into a tmp dir to ensure no .imp/ ambient leakage
    staged = tmp_path / "wf"
    shutil.copytree(default_workflow_dir(), staged)
    # Tiers referenced by the default workflow
    configured = {"low", "medium", "high"}
    workflow = load_workflow(staged, configured_tiers=configured)
    assert workflow.name == "feature"
    assert [p.name for p in workflow.phases] == [
        "spec", "plan", "implement", "review", "merge",
    ]


def test_default_workflow_fails_with_missing_role(tmp_path: Path) -> None:
    staged = tmp_path / "wf"
    shutil.copytree(default_workflow_dir(), staged)
    with pytest.raises(LoadErrors) as exc:
        load_workflow(staged, configured_tiers=frozenset({"medium"}))
    msg = str(exc.value)
    assert "low" in msg or "high" in msg  # at least one undefined tier surfaces

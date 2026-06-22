from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from imp.assets import bundled_workflow_dir


def test_permissions_module_is_deleted():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("imp.agents.permissions")


def test_fs_rule_type_is_gone():
    import imp.engine.types as t
    assert not hasattr(t, "FsRule")


def test_step_has_no_fs_permissions_field():
    from imp.engine.types import Step
    assert "fs_permissions" not in Step.__dataclass_fields__


def test_no_fs_permissions_in_bundled_workflows():
    offenders: list[str] = []
    for wf in ("imp", "patent"):
        root = Path(bundled_workflow_dir(wf))
        for f in root.rglob("*.yaml"):
            for i, line in enumerate(f.read_text().splitlines(), start=1):
                if "fs_permissions" in line:
                    offenders.append(f"{wf}/{f.name}:{i}")
    assert not offenders, f"fs_permissions still present: {offenders}"

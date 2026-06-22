"""Bundled-asset accessor + the default-workflow resolution seam (spec items 1, 2)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from imp.assets import bundled_workflow_dir, is_bundled_workflow
from imp.engine.change import Change
from imp.engine.errors import LoadError
from imp.providers.config import parse_default_workflow
from tests.conftest import _git, _seed_control


def test_bundled_workflow_dir_resolves_imp() -> None:
    d = bundled_workflow_dir("imp")
    assert d.is_dir()
    assert (d / "workflow.yaml").exists()


def test_bundled_workflow_dir_missing_raises() -> None:
    with pytest.raises(RuntimeError):
        bundled_workflow_dir("does-not-exist")


def test_is_bundled_workflow_predicate() -> None:
    assert is_bundled_workflow("imp") is True
    assert is_bundled_workflow("does-not-exist") is False


def test_parse_default_workflow_defaults_to_imp(tmp_path: Path) -> None:
    assert parse_default_workflow(tmp_path / "absent.yaml") == "imp"
    p = tmp_path / "project.yaml"
    p.write_text("vars: {}\n")
    assert parse_default_workflow(p) == "imp"


def test_parse_default_workflow_reads_field(tmp_path: Path) -> None:
    p = tmp_path / "project.yaml"
    p.write_text("default-workflow: mine\n")
    assert parse_default_workflow(p) == "mine"


def test_parse_default_workflow_rejects_path_traversal(tmp_path: Path) -> None:
    p = tmp_path / "project.yaml"
    p.write_text("default-workflow: ../escape\n")
    with pytest.raises(LoadError):
        parse_default_workflow(p)


def test_change_resolves_fork_workflow_dir(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    shutil.copytree(tmp_path / "imp" / "workflows" / "imp",
                    tmp_path / "imp" / "workflows" / "mine")
    (tmp_path / "imp" / "project.yaml").write_text("default-workflow: mine\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "fork workflow")
    ch = Change.create(tmp_path, title="X", change_id="2026-06-15-fork01",
                       base_branch="main")
    assert ch.workflow_dir == ch.project_root / "imp" / "workflows" / "mine"


def test_change_missing_workflow_dir_raises_naming_path(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    (tmp_path / "imp" / "project.yaml").write_text("default-workflow: ghost\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "point at missing")
    with pytest.raises(LoadError) as exc:
        Change.create(tmp_path, title="X", change_id="2026-06-15-ghost1",
                      base_branch="main")
    assert "workflows/ghost" in str(exc.value).replace("\\", "/")


def test_validate_workflow_name_accepts_plain_segment() -> None:
    from imp.providers.config import validate_workflow_name
    assert validate_workflow_name("imp") is True
    assert validate_workflow_name("my-fork") is True


def test_validate_workflow_name_rejects_bad_segments() -> None:
    from imp.providers.config import validate_workflow_name
    for bad in ["", "   ", "a/b", "a\\b", ".hidden"]:
        assert validate_workflow_name(bad) is False


def test_resolve_workflow_dir_prefers_committed(tmp_path: Path) -> None:
    from imp.assets import resolve_workflow_dir
    committed = tmp_path / "imp" / "workflows" / "imp"
    committed.mkdir(parents=True)
    (committed / "workflow.yaml").write_text("name: fork\n")
    assert resolve_workflow_dir(tmp_path, "imp") == committed


def test_resolve_workflow_dir_falls_back_to_bundled(tmp_path: Path) -> None:
    from imp.assets import bundled_workflow_dir, resolve_workflow_dir
    # Nothing committed under tmp_path -> the installed bundled workflow.
    assert resolve_workflow_dir(tmp_path, "imp") == bundled_workflow_dir("imp")


def test_resolve_workflow_dir_unknown_names_valid_set(tmp_path: Path) -> None:
    from imp.assets import resolve_workflow_dir
    with pytest.raises(LoadError) as exc:
        resolve_workflow_dir(tmp_path, "bogus")
    msg = str(exc.value)
    assert "bogus" in msg
    assert "imp" in msg and "patent" in msg  # bundled set is named in the error


def test_available_workflow_names_unions_bundled_and_committed(tmp_path: Path) -> None:
    from imp.assets import available_workflow_names
    (tmp_path / "imp" / "workflows" / "mine").mkdir(parents=True)
    names = available_workflow_names(tmp_path)
    assert {"imp", "patent", "mine"} <= set(names)
    assert names == sorted(names)

"""Bundled-asset accessor + the default-workflow resolution seam (spec items 1, 2)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from imp.assets import bundled_workflow_dir, is_bundled_workflow
from imp.engine.errors import LoadError
from imp.engine.quest import Quest
from imp.providers.config import parse_default_base, parse_default_workflow
from imp.providers.effective import resolve_default_workflow
from tests.conftest import _git, _seed_control


def test_bundled_workflow_dir_resolves_default() -> None:
    d = bundled_workflow_dir("default")
    assert d.is_dir()
    assert (d / "workflow.yaml").exists()


def test_bundled_workflow_dir_missing_raises() -> None:
    with pytest.raises(RuntimeError):
        bundled_workflow_dir("does-not-exist")


def test_is_bundled_workflow_predicate() -> None:
    assert is_bundled_workflow("default") is True
    assert is_bundled_workflow("does-not-exist") is False


def test_parse_default_workflow_defaults_to_default(tmp_path: Path) -> None:
    assert parse_default_workflow(tmp_path / "absent.yaml") == "default"
    p = tmp_path / "config.yaml"
    p.write_text("vars: {}\n")
    assert parse_default_workflow(p) == "default"


def test_parse_default_workflow_reads_field(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("default-workflow: mine\n")
    assert parse_default_workflow(p) == "mine"


def test_parse_default_workflow_rejects_path_traversal(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("default-workflow: ../escape\n")
    with pytest.raises(LoadError):
        parse_default_workflow(p)


def test_change_resolves_fork_workflow_dir(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    shutil.copytree(tmp_path / "imp" / "workflows" / "imp",
                    tmp_path / "imp" / "workflows" / "mine")
    (tmp_path / "imp" / "config.yaml").write_text("default-workflow: mine\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "fork workflow")
    ch = Quest.create(tmp_path, title="X", quest_id="2026-06-15-fork01",
                       base_branch="main")
    assert ch.workflow_dir == ch.project_root / "imp" / "workflows" / "mine"


def test_change_missing_workflow_dir_raises_naming_path(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    (tmp_path / "imp" / "config.yaml").write_text("default-workflow: ghost\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "point at missing")
    with pytest.raises(LoadError) as exc:
        Quest.create(tmp_path, title="X", quest_id="2026-06-15-ghost1",
                      base_branch="main")
    msg = str(exc.value)
    assert "ghost" in msg
    assert "imp" in msg and "patent" in msg


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
    assert resolve_workflow_dir(tmp_path, "default") == bundled_workflow_dir("default")


def test_resolve_workflow_dir_unknown_names_valid_set(tmp_path: Path) -> None:
    from imp.assets import resolve_workflow_dir
    with pytest.raises(LoadError) as exc:
        resolve_workflow_dir(tmp_path, "bogus")
    msg = str(exc.value)
    assert "bogus" in msg
    assert "default" in msg and "patent" in msg  # bundled set is named in the error


def test_resolve_workflow_dir_rejects_invalid_name(tmp_path: Path) -> None:
    from imp.assets import resolve_workflow_dir
    with pytest.raises(LoadError):
        resolve_workflow_dir(tmp_path, "../evil")
    with pytest.raises(LoadError):
        resolve_workflow_dir(tmp_path, "bad/name")


def test_parse_default_base_absent_file_is_none(tmp_path: Path) -> None:
    assert parse_default_base(tmp_path / "absent.yaml") is None


def test_parse_default_base_missing_key_is_none(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("default-workflow: imp\n")
    assert parse_default_base(p) is None


def test_parse_default_base_reads_field(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("default-base: master\n")
    assert parse_default_base(p) == "master"


def test_parse_default_base_allows_slashes(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("default-base: release/2026\n")
    assert parse_default_base(p) == "release/2026"


def test_parse_default_base_blank_value_raises(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("default-base: '   '\n")
    with pytest.raises(LoadError):
        parse_default_base(p)


def test_parse_default_base_non_string_raises(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("default-base: [a, b]\n")
    with pytest.raises(LoadError):
        parse_default_base(p)


def test_parse_default_base_malformed_yaml_raises_load_error(tmp_path: Path) -> None:
    # ruamel's YAMLError is unrelated to PyYAML's, so an unswapped except clause
    # would let a raw ruamel traceback escape instead of imp's LoadError.
    p = tmp_path / "config.yaml"
    p.write_text("default-base: [master\n")
    with pytest.raises(LoadError) as exc:
        parse_default_base(p)
    assert "invalid YAML in config.yaml" in str(exc.value)


def test_available_workflow_names_unions_bundled_and_committed(tmp_path: Path) -> None:
    from imp.assets import available_workflow_names
    (tmp_path / "imp" / "workflows" / "mine").mkdir(parents=True)
    names = available_workflow_names(tmp_path)
    assert {"default", "patent", "mine"} <= set(names)
    assert names == sorted(names)


def test_selectable_workflow_names_drops_internal_workflows(tmp_path: Path) -> None:
    from imp.assets import (
        INTERNAL_WORKFLOWS,
        available_workflow_names,
        bundled_workflow_names,
        resolve_workflow_dir,
        selectable_workflow_names,
    )
    names = selectable_workflow_names(tmp_path)
    assert names == sorted(set(bundled_workflow_names()) - INTERNAL_WORKFLOWS)
    assert "memory" not in names
    # the full set is deliberately unchanged: memory-tree validation and quest
    # creation (`imp new --workflow memory --background`) both need it
    assert "memory" in available_workflow_names(tmp_path)
    assert resolve_workflow_dir(tmp_path, "memory").is_dir()


def test_selectable_workflow_names_include_committed_fork(tmp_path: Path) -> None:
    from imp.assets import selectable_workflow_names
    (tmp_path / "imp" / "workflows" / "mine").mkdir(parents=True)
    names = selectable_workflow_names(tmp_path)
    assert "mine" in names
    assert "memory" not in names
    assert names == sorted(names)


def test_resolve_default_workflow_defaults_when_no_layer_sets_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    proj = tmp_path / "proj"
    (proj / "imp").mkdir(parents=True)
    (proj / "imp" / "config.yaml").write_text("theme: dark\n")
    assert resolve_default_workflow(project_root=proj, control_root=None) == "default"


def test_resolve_default_workflow_reads_the_project_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    proj = tmp_path / "proj"
    (proj / "imp").mkdir(parents=True)
    (proj / "imp" / "config.yaml").write_text("default-workflow: mine\n")
    assert resolve_default_workflow(project_root=proj, control_root=None) == "mine"


def test_resolve_default_workflow_global_beats_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    imp_home = tmp_path / "imphome"
    imp_home.mkdir(parents=True)
    (imp_home / "config.yaml").write_text("default-workflow: fromglobal\n")
    monkeypatch.setenv("IMP_HOME", str(imp_home))
    proj = tmp_path / "proj"
    (proj / "imp").mkdir(parents=True)
    (proj / "imp" / "config.yaml").write_text("default-workflow: fromproject\n")
    assert resolve_default_workflow(
        project_root=proj, control_root=None) == "fromglobal"


def test_resolve_default_workflow_local_layer_needs_a_control_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """control_root=None (the subagents.py fallback) drops the local layer and
    keeps everything below it."""
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    proj = tmp_path / "proj"
    (proj / "imp").mkdir(parents=True)
    (proj / "imp" / "config.yaml").write_text("default-workflow: fromproject\n")
    (proj / ".imp").mkdir()
    (proj / ".imp" / "config.yaml").write_text("default-workflow: fromlocal\n")
    assert resolve_default_workflow(
        project_root=proj, control_root=None) == "fromproject"
    assert resolve_default_workflow(
        project_root=proj, control_root=proj) == "fromlocal"

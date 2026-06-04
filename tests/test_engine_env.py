import os
from pathlib import Path

from imp.engine.env import engine_env, overlay_engine_env


def test_engine_env_exposes_slug_and_title_no_artifacts() -> None:
    env = engine_env(project_root=Path("/wt"), change_slug="add-oauth",
                     change_title="Add OAuth")
    assert env == {
        "IMP_PROJECT_ROOT": "/wt",
        "IMP_CHANGE_SLUG": "add-oauth",
        "IMP_CHANGE_TITLE": "Add OAuth",
    }
    assert "IMP_ARTIFACTS_DIR" not in env


def test_engine_env_injects_lint_command_when_set() -> None:
    env = engine_env(project_root=Path("/wt"), change_slug="s",
                     change_title="t", lint_command="make lint")
    assert env["IMP_LINT_COMMAND"] == "make lint"


def test_engine_env_omits_lint_command_when_none() -> None:
    env = engine_env(project_root=Path("/wt"), change_slug="s",
                     change_title="t")
    assert "IMP_LINT_COMMAND" not in env


def test_overlay_strips_inherited_lint_command_when_unset() -> None:
    # engine_env is the authority: an inherited IMP_LINT_COMMAND must be dropped
    # when the overlay (no lint_command) doesn't provide it — no ambient leak.
    parent = {"IMP_LINT_COMMAND": "make stale", "PATH": "/usr/bin", "FOO": "bar"}
    overlay = engine_env(project_root=Path("/wt"), change_slug="s", change_title="t")
    env = overlay_engine_env(parent, overlay)
    assert "IMP_LINT_COMMAND" not in env      # inherited copy stripped
    assert env["FOO"] == "bar"                # non-engine vars preserved
    assert env["IMP_PROJECT_ROOT"] == "/wt"   # engine var applied from overlay


def test_overlay_configured_lint_command_overrides_inherited() -> None:
    parent = {"IMP_LINT_COMMAND": "make stale"}
    overlay = engine_env(project_root=Path("/wt"), change_slug="s",
                         change_title="t", lint_command="make lint")
    env = overlay_engine_env(parent, overlay)
    assert env["IMP_LINT_COMMAND"] == "make lint"   # overlay wins over inherited


def test_engine_env_injects_test_command_when_set() -> None:
    env = engine_env(project_root=Path("/wt"), change_slug="s",
                     change_title="t", test_command="make test")
    assert env["IMP_TEST_COMMAND"] == "make test"


def test_engine_env_omits_test_command_when_none() -> None:
    env = engine_env(project_root=Path("/wt"), change_slug="s",
                     change_title="t")
    assert "IMP_TEST_COMMAND" not in env


def test_overlay_strips_inherited_test_command_when_unset() -> None:
    # engine_env is the authority: an inherited IMP_TEST_COMMAND must be dropped
    # when the overlay (no test_command) doesn't provide it — no ambient leak.
    parent = {"IMP_TEST_COMMAND": "make stale", "PATH": "/usr/bin", "FOO": "bar"}
    overlay = engine_env(project_root=Path("/wt"), change_slug="s", change_title="t")
    env = overlay_engine_env(parent, overlay)
    assert "IMP_TEST_COMMAND" not in env      # inherited copy stripped
    assert env["FOO"] == "bar"                # non-engine vars preserved
    assert env["IMP_PROJECT_ROOT"] == "/wt"   # engine var applied from overlay


def test_overlay_configured_test_command_overrides_inherited() -> None:
    parent = {"IMP_TEST_COMMAND": "make stale"}
    overlay = engine_env(project_root=Path("/wt"), change_slug="s",
                         change_title="t", test_command="make test")
    env = overlay_engine_env(parent, overlay)
    assert env["IMP_TEST_COMMAND"] == "make test"   # overlay wins over inherited


def _make_imp_checkout(root: Path) -> None:
    """Lay down the minimal imp source-checkout marker (src/imp/__init__.py)."""
    pkg = root / "src" / "imp"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")


def test_engine_env_injects_worktree_src_on_pythonpath_for_imp_checkout(
    tmp_path: Path,
) -> None:
    # imp-on-imp: an imp source checkout => PYTHONPATH prefix points at the
    # worktree's src so the editable install can't resolve `import imp` to a
    # sibling checkout.
    _make_imp_checkout(tmp_path)
    env = engine_env(project_root=tmp_path, change_slug="s", change_title="t")
    assert env["PYTHONPATH"] == str(tmp_path / "src")


def test_engine_env_omits_pythonpath_without_src_layout(tmp_path: Path) -> None:
    env = engine_env(project_root=tmp_path, change_slug="s", change_title="t")
    assert "PYTHONPATH" not in env


def test_engine_env_omits_pythonpath_for_non_imp_src_layout(tmp_path: Path) -> None:
    # A bare src/ dir, or a non-imp src-layout project, must NOT get its src/
    # forced onto subprocess PYTHONPATH — the shadow is imp-on-imp only and must
    # never perturb sys.path for unrelated projects' gate/agent subprocesses.
    (tmp_path / "src" / "otherpkg").mkdir(parents=True)
    (tmp_path / "src" / "otherpkg" / "__init__.py").write_text("")
    env = engine_env(project_root=tmp_path, change_slug="s", change_title="t")
    assert "PYTHONPATH" not in env


def test_overlay_prepends_worktree_src_to_inherited_pythonpath(
    tmp_path: Path,
) -> None:
    _make_imp_checkout(tmp_path)
    parent = {"PYTHONPATH": "/tmp/ambient"}
    overlay = engine_env(project_root=tmp_path, change_slug="s", change_title="t")
    env = overlay_engine_env(parent, overlay)
    # worktree src first (wins over the editable .pth), ambient preserved after.
    assert env["PYTHONPATH"] == f"{tmp_path / 'src'}{os.pathsep}/tmp/ambient"


def test_overlay_sets_pythonpath_alone_when_none_inherited(tmp_path: Path) -> None:
    _make_imp_checkout(tmp_path)
    overlay = engine_env(project_root=tmp_path, change_slug="s", change_title="t")
    env = overlay_engine_env({}, overlay)
    assert env["PYTHONPATH"] == str(tmp_path / "src")


def test_overlay_dedups_worktree_src_already_in_inherited_pythonpath(
    tmp_path: Path,
) -> None:
    # Nested imp-on-imp (or a re-entered worktree) can inherit a PYTHONPATH that
    # already starts with this src/. Prepend must not duplicate it.
    _make_imp_checkout(tmp_path)
    src = str(tmp_path / "src")
    parent = {"PYTHONPATH": f"{src}{os.pathsep}/tmp/ambient"}
    overlay = engine_env(project_root=tmp_path, change_slug="s", change_title="t")
    env = overlay_engine_env(parent, overlay)
    assert env["PYTHONPATH"] == f"{src}{os.pathsep}/tmp/ambient"
    assert env["PYTHONPATH"].count(src) == 1

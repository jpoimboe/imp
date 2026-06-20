import os
from pathlib import Path

from imp.engine.env import engine_env, overlay_engine_env
from tests.conftest import _make_imp_checkout


def test_engine_env_exposes_handle_and_title_no_artifacts() -> None:
    env = engine_env(project_root=Path("/wt"), change_id="2026-06-04-eee555",
                     change_handle="add-oauth", change_title="Add OAuth")
    assert env == {
        "IMP_PROJECT_ROOT": "/wt",
        "IMP_CHANGE_ID": "2026-06-04-eee555",
        "IMP_CHANGE_HANDLE": "add-oauth",
        "IMP_CHANGE_TITLE": "Add OAuth",
    }
    assert "IMP_ARTIFACTS_DIR" not in env


def test_engine_env_exposes_change_id() -> None:
    env = engine_env(project_root=Path("/wt"), change_id="2026-06-04-eee555",
                     change_handle="s", change_title="t")
    assert env["IMP_CHANGE_ID"] == "2026-06-04-eee555"


def test_engine_env_injects_verify_command_when_set() -> None:
    env = engine_env(project_root=Path("/wt"), change_id="2026-06-04-eee555", change_handle="s",
                     change_title="t", verify_command="make check")
    assert env["IMP_VERIFY_COMMAND"] == "make check"


def test_engine_env_omits_verify_command_when_none() -> None:
    env = engine_env(project_root=Path("/wt"), change_id="2026-06-04-eee555", change_handle="s",
                     change_title="t")
    assert "IMP_VERIFY_COMMAND" not in env


def test_engine_env_omits_verify_command_when_empty() -> None:
    # Falsy command (opted-out "") is treated as absent => var omitted => gate no-ops.
    env = engine_env(project_root=Path("/wt"), change_id="2026-06-04-eee555", change_handle="s",
                     change_title="t", verify_command="")
    assert "IMP_VERIFY_COMMAND" not in env


def test_overlay_strips_inherited_verify_command_when_unset() -> None:
    parent = {"IMP_VERIFY_COMMAND": "make stale", "PATH": "/usr/bin", "FOO": "bar"}
    overlay = engine_env(project_root=Path("/wt"), change_id="2026-06-04-eee555",
                         change_handle="s", change_title="t")
    env = overlay_engine_env(parent, overlay)
    assert "IMP_VERIFY_COMMAND" not in env      # inherited copy stripped
    assert env["FOO"] == "bar"
    assert env["IMP_PROJECT_ROOT"] == "/wt"


def test_overlay_configured_verify_command_overrides_inherited() -> None:
    parent = {"IMP_VERIFY_COMMAND": "make stale"}
    overlay = engine_env(project_root=Path("/wt"), change_id="2026-06-04-eee555", change_handle="s",
                         change_title="t", verify_command="make check")
    env = overlay_engine_env(parent, overlay)
    assert env["IMP_VERIFY_COMMAND"] == "make check"


def test_engine_env_injects_verify_timeout_as_string_when_set() -> None:
    env = engine_env(project_root=Path("/wt"), change_id="2026-06-04-eee555", change_handle="s",
                     change_title="t", verify_timeout=1200)
    assert env["IMP_VERIFY_TIMEOUT"] == "1200"   # run-verify.sh reads a string


def test_engine_env_omits_verify_timeout_when_none() -> None:
    # Absent => run-verify.sh falls back to its own 300s default.
    env = engine_env(project_root=Path("/wt"), change_id="2026-06-04-eee555", change_handle="s",
                     change_title="t")
    assert "IMP_VERIFY_TIMEOUT" not in env


def test_overlay_strips_inherited_verify_timeout_when_unset() -> None:
    # A stale ambient IMP_VERIFY_TIMEOUT (e.g. imp-on-imp) must not leak past an
    # unset config — else a parent's ceiling would silently govern the child gate.
    parent = {"IMP_VERIFY_TIMEOUT": "60", "FOO": "bar"}
    overlay = engine_env(project_root=Path("/wt"), change_id="2026-06-04-eee555",
                         change_handle="s", change_title="t")
    env = overlay_engine_env(parent, overlay)
    assert "IMP_VERIFY_TIMEOUT" not in env
    assert env["FOO"] == "bar"


def test_overlay_configured_verify_timeout_overrides_inherited() -> None:
    parent = {"IMP_VERIFY_TIMEOUT": "60"}
    overlay = engine_env(project_root=Path("/wt"), change_id="2026-06-04-eee555", change_handle="s",
                         change_title="t", verify_timeout=1200)
    env = overlay_engine_env(parent, overlay)
    assert env["IMP_VERIFY_TIMEOUT"] == "1200"


def test_engine_env_injects_worktree_src_on_pythonpath_for_imp_checkout(
    tmp_path: Path,
) -> None:
    # imp-on-imp: an imp source checkout => PYTHONPATH prefix points at the
    # worktree's src so the editable install can't resolve `import imp` to a
    # sibling checkout.
    _make_imp_checkout(tmp_path)
    env = engine_env(project_root=tmp_path, change_id="2026-06-04-eee555",
                     change_handle="s", change_title="t")
    assert env["PYTHONPATH"] == str(tmp_path / "src")


def test_engine_env_omits_pythonpath_without_src_layout(tmp_path: Path) -> None:
    env = engine_env(project_root=tmp_path, change_id="2026-06-04-eee555",
                     change_handle="s", change_title="t")
    assert "PYTHONPATH" not in env


def test_engine_env_omits_pythonpath_for_non_imp_src_layout(tmp_path: Path) -> None:
    # A bare src/ dir, or a non-imp src-layout project, must NOT get its src/
    # forced onto subprocess PYTHONPATH — the shadow is imp-on-imp only and must
    # never perturb sys.path for unrelated projects' gate/agent subprocesses.
    (tmp_path / "src" / "otherpkg").mkdir(parents=True)
    (tmp_path / "src" / "otherpkg" / "__init__.py").write_text("")
    env = engine_env(project_root=tmp_path, change_id="2026-06-04-eee555",
                     change_handle="s", change_title="t")
    assert "PYTHONPATH" not in env


def test_overlay_prepends_worktree_src_to_inherited_pythonpath(
    tmp_path: Path,
) -> None:
    _make_imp_checkout(tmp_path)
    parent = {"PYTHONPATH": "/tmp/ambient"}
    overlay = engine_env(project_root=tmp_path, change_id="2026-06-04-eee555",
                     change_handle="s", change_title="t")
    env = overlay_engine_env(parent, overlay)
    # worktree src first (wins over the editable .pth), ambient preserved after.
    assert env["PYTHONPATH"] == f"{tmp_path / 'src'}{os.pathsep}/tmp/ambient"


def test_overlay_sets_pythonpath_alone_when_none_inherited(tmp_path: Path) -> None:
    _make_imp_checkout(tmp_path)
    overlay = engine_env(project_root=tmp_path, change_id="2026-06-04-eee555",
                     change_handle="s", change_title="t")
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
    overlay = engine_env(project_root=tmp_path, change_id="2026-06-04-eee555",
                     change_handle="s", change_title="t")
    env = overlay_engine_env(parent, overlay)
    assert env["PYTHONPATH"] == f"{src}{os.pathsep}/tmp/ambient"
    assert env["PYTHONPATH"].count(src) == 1


def test_overlay_strips_inherited_change_dir_when_unset() -> None:
    stale = {"IMP_CHANGE_DIR": "/old/home", "OTHER": "x"}
    result = overlay_engine_env(
        stale,
        engine_env(project_root=Path("/wt"), change_id="2026-06-04-eee555",
                   change_handle="s", change_title="t"),
    )
    assert "IMP_CHANGE_DIR" not in result


def test_engine_env_includes_change_dir_when_given() -> None:
    env = engine_env(project_root=Path("/wt"), change_id="2026-06-04-eee555",
                     change_handle="s", change_title="t",
                     change_dir=Path("/ctl/.imp/changes/2026-06-04-eee555"))
    assert env["IMP_CHANGE_DIR"] == "/ctl/.imp/changes/2026-06-04-eee555"


def test_engine_env_omits_change_dir_when_absent() -> None:
    env = engine_env(project_root=Path("/wt"), change_id="2026-06-04-eee555",
                     change_handle="s", change_title="t")
    assert "IMP_CHANGE_DIR" not in env


def test_change_dir_is_engine_managed() -> None:
    from imp.engine.env import ENGINE_ENV_VARS
    assert "IMP_CHANGE_DIR" in ENGINE_ENV_VARS

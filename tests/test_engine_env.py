import os
from pathlib import Path

from imp.engine.env import TestGates, engine_env, overlay_engine_env
from tests.conftest import _make_imp_checkout


def test_engine_env_exposes_id_and_title_no_artifacts() -> None:
    env = engine_env(project_root=Path("/wt"), change_id="eee555",
                     change_title="Add OAuth")
    assert env == {
        "IMP_PROJECT_ROOT": "/wt",
        "IMP_CHANGE_ID": "eee555",
        "IMP_CHANGE_TITLE": "Add OAuth",
    }
    assert "IMP_ARTIFACTS_DIR" not in env


def test_engine_env_exposes_change_id() -> None:
    env = engine_env(project_root=Path("/wt"), change_id="2026-06-04-eee555",
                     change_title="t")
    assert env["IMP_CHANGE_ID"] == "2026-06-04-eee555"


def test_engine_env_injects_worktree_src_on_pythonpath_for_imp_checkout(
    tmp_path: Path,
) -> None:
    # imp-on-imp: an imp source checkout => PYTHONPATH prefix points at the
    # worktree's src so the editable install can't resolve `import imp` to a
    # sibling checkout.
    _make_imp_checkout(tmp_path)
    env = engine_env(project_root=tmp_path, change_id="2026-06-04-eee555",
                     change_title="t")
    assert env["PYTHONPATH"] == str(tmp_path / "src")


def test_engine_env_omits_pythonpath_without_src_layout(tmp_path: Path) -> None:
    env = engine_env(project_root=tmp_path, change_id="2026-06-04-eee555",
                     change_title="t")
    assert "PYTHONPATH" not in env


def test_engine_env_omits_pythonpath_for_non_imp_src_layout(tmp_path: Path) -> None:
    # A bare src/ dir, or a non-imp src-layout project, must NOT get its src/
    # forced onto subprocess PYTHONPATH — the shadow is imp-on-imp only and must
    # never perturb sys.path for unrelated projects' gate/agent subprocesses.
    (tmp_path / "src" / "otherpkg").mkdir(parents=True)
    (tmp_path / "src" / "otherpkg" / "__init__.py").write_text("")
    env = engine_env(project_root=tmp_path, change_id="2026-06-04-eee555",
                     change_title="t")
    assert "PYTHONPATH" not in env


def test_overlay_prepends_worktree_src_to_inherited_pythonpath(
    tmp_path: Path,
) -> None:
    _make_imp_checkout(tmp_path)
    parent = {"PYTHONPATH": "/tmp/ambient"}
    overlay = engine_env(project_root=tmp_path, change_id="2026-06-04-eee555",
                     change_title="t")
    env = overlay_engine_env(parent, overlay)
    # worktree src first (wins over the editable .pth), ambient preserved after.
    assert env["PYTHONPATH"] == f"{tmp_path / 'src'}{os.pathsep}/tmp/ambient"


def test_overlay_sets_pythonpath_alone_when_none_inherited(tmp_path: Path) -> None:
    _make_imp_checkout(tmp_path)
    overlay = engine_env(project_root=tmp_path, change_id="2026-06-04-eee555",
                     change_title="t")
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
                     change_title="t")
    env = overlay_engine_env(parent, overlay)
    assert env["PYTHONPATH"] == f"{src}{os.pathsep}/tmp/ambient"
    assert env["PYTHONPATH"].count(src) == 1


def test_overlay_strips_inherited_change_dir_when_unset() -> None:
    stale = {"IMP_CHANGE_DIR": "/old/home", "OTHER": "x"}
    result = overlay_engine_env(
        stale,
        engine_env(project_root=Path("/wt"), change_id="2026-06-04-eee555",
                   change_title="t"),
    )
    assert "IMP_CHANGE_DIR" not in result


def test_engine_env_includes_change_dir_when_given() -> None:
    env = engine_env(project_root=Path("/wt"), change_id="2026-06-04-eee555",
                     change_title="t",
                     change_dir=Path("/ctl/.imp/changes/2026-06-04-eee555"))
    assert env["IMP_CHANGE_DIR"] == "/ctl/.imp/changes/2026-06-04-eee555"


def test_engine_env_omits_change_dir_when_absent() -> None:
    env = engine_env(project_root=Path("/wt"), change_id="2026-06-04-eee555",
                     change_title="t")
    assert "IMP_CHANGE_DIR" not in env


def test_change_dir_is_engine_managed() -> None:
    from imp.engine.env import ENGINE_ENV_VARS
    assert "IMP_CHANGE_DIR" in ENGINE_ENV_VARS


def test_engine_env_injects_test_cmd_fast_when_set() -> None:
    env = engine_env(project_root=Path("/wt"), change_id="c", change_title="t",
                     test_gates=TestGates(fast="make check"))
    assert env["IMP_TEST_CMD_FAST"] == "make check"


def test_engine_env_injects_test_cmd_slow_when_set() -> None:
    env = engine_env(project_root=Path("/wt"), change_id="c", change_title="t",
                     test_gates=TestGates(slow="make check && make test-live"))
    assert env["IMP_TEST_CMD_SLOW"] == "make check && make test-live"


def test_engine_env_injects_test_cmd_timeouts_as_strings() -> None:
    env = engine_env(project_root=Path("/wt"), change_id="c", change_title="t",
                     test_gates=TestGates(fast_timeout=120, slow_timeout=300))
    assert env["IMP_TEST_CMD_FAST_TIMEOUT"] == "120"   # run-verify.sh reads a string
    assert env["IMP_TEST_CMD_SLOW_TIMEOUT"] == "300"


def test_engine_env_omits_test_cmd_vars_without_test_gates() -> None:
    env = engine_env(project_root=Path("/wt"), change_id="c", change_title="t")
    for v in ("IMP_TEST_CMD_FAST", "IMP_TEST_CMD_SLOW",
              "IMP_TEST_CMD_FAST_TIMEOUT", "IMP_TEST_CMD_SLOW_TIMEOUT"):
        assert v not in env


def test_engine_env_omits_test_cmd_fast_when_empty() -> None:
    # Falsy command (opted-out "") is treated as absent => var omitted.
    env = engine_env(project_root=Path("/wt"), change_id="c", change_title="t",
                     test_gates=TestGates(fast=""))
    assert "IMP_TEST_CMD_FAST" not in env


def test_overlay_strips_inherited_test_cmd_fast_when_unset() -> None:
    # A stale ambient IMP_TEST_CMD_FAST (imp-on-imp) must not leak past an unset
    # bundle — engine_env is authoritative; overlay strips the inherited copy.
    parent = {"IMP_TEST_CMD_FAST": "make stale", "FOO": "bar"}
    overlay = engine_env(project_root=Path("/wt"), change_id="c", change_title="t")
    env = overlay_engine_env(parent, overlay)
    assert "IMP_TEST_CMD_FAST" not in env
    assert env["FOO"] == "bar"


def test_overlay_configured_test_cmd_fast_overrides_inherited() -> None:
    parent = {"IMP_TEST_CMD_FAST": "make stale"}
    overlay = engine_env(project_root=Path("/wt"), change_id="c", change_title="t",
                         test_gates=TestGates(fast="make check"))
    env = overlay_engine_env(parent, overlay)
    assert env["IMP_TEST_CMD_FAST"] == "make check"


def test_test_cmd_vars_are_engine_managed() -> None:
    from imp.engine.env import ENGINE_ENV_VARS
    for v in ("IMP_TEST_CMD_FAST", "IMP_TEST_CMD_SLOW",
              "IMP_TEST_CMD_FAST_TIMEOUT", "IMP_TEST_CMD_SLOW_TIMEOUT"):
        assert v in ENGINE_ENV_VARS


def test_test_gates_from_config_reads_test_cmd_fields() -> None:
    import types
    cfg = types.SimpleNamespace(
        test_cmd_fast="make check", test_cmd_slow="make check && make test-live",
        test_cmd_fast_timeout=120, test_cmd_slow_timeout=300)
    gates = TestGates.from_config(cfg)
    assert gates == TestGates(fast="make check",
                              slow="make check && make test-live",
                              fast_timeout=120, slow_timeout=300)


def test_engine_env_injects_zero_timeout() -> None:
    # Guard the `is not None` semantics: a 0 timeout must still be emitted, not
    # dropped by a truthiness check.
    env = engine_env(project_root=Path("/wt"), change_id="c", change_title="t",
                     test_gates=TestGates(fast_timeout=0, slow_timeout=0))
    assert env["IMP_TEST_CMD_FAST_TIMEOUT"] == "0"
    assert env["IMP_TEST_CMD_SLOW_TIMEOUT"] == "0"


def test_engine_env_omits_test_cmd_slow_when_empty() -> None:
    env = engine_env(project_root=Path("/wt"), change_id="c", change_title="t",
                     test_gates=TestGates(slow=""))
    assert "IMP_TEST_CMD_SLOW" not in env


def test_overlay_strips_all_inherited_test_cmd_vars_when_unset() -> None:
    parent = {"IMP_TEST_CMD_FAST": "make stale", "IMP_TEST_CMD_SLOW": "make stale",
              "IMP_TEST_CMD_FAST_TIMEOUT": "999", "IMP_TEST_CMD_SLOW_TIMEOUT": "999"}
    overlay = engine_env(project_root=Path("/wt"), change_id="c", change_title="t")
    env = overlay_engine_env(parent, overlay)
    for v in ("IMP_TEST_CMD_FAST", "IMP_TEST_CMD_SLOW",
              "IMP_TEST_CMD_FAST_TIMEOUT", "IMP_TEST_CMD_SLOW_TIMEOUT"):
        assert v not in env


def test_engine_env_injects_output_path_when_given() -> None:
    env = engine_env(project_root=Path("/wt"), change_id="c", change_title="t",
                     output_path=Path("/tmp/imp-output-abc.json"))
    assert env["IMP_OUTPUT"] == "/tmp/imp-output-abc.json"


def test_engine_env_omits_output_path_when_absent() -> None:
    env = engine_env(project_root=Path("/wt"), change_id="c", change_title="t")
    assert "IMP_OUTPUT" not in env


def test_output_path_var_is_engine_managed() -> None:
    from imp.engine.env import ENGINE_ENV_VARS
    assert "IMP_OUTPUT" in ENGINE_ENV_VARS

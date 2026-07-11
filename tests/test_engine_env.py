import os
from pathlib import Path

from imp.engine.env import TestGates, engine_env, overlay_engine_env
from tests.conftest import _make_imp_checkout


def test_engine_env_exposes_id_and_title_no_artifacts() -> None:
    env = engine_env(project_root=Path("/wt"), quest_id="eee555",
                     quest_title="Add OAuth")
    assert env == {
        "IMP_PROJECT_ROOT": "/wt",
        "IMP_QUEST_ID": "eee555",
        "IMP_QUEST_TITLE": "Add OAuth",
        "GIT_EDITOR": "true",
        "GIT_TERMINAL_PROMPT": "0",
        "EDITOR": "true",
        "VISUAL": "true",
    }
    assert "IMP_ARTIFACTS_DIR" not in env


def test_engine_env_exposes_quest_id() -> None:
    env = engine_env(project_root=Path("/wt"), quest_id="2026-06-04-eee555",
                     quest_title="t")
    assert env["IMP_QUEST_ID"] == "2026-06-04-eee555"


def test_engine_env_injects_worktree_src_on_pythonpath_for_imp_checkout(
    tmp_path: Path,
) -> None:
    # imp-on-imp: an imp source checkout => PYTHONPATH prefix points at the
    # worktree's src so the editable install can't resolve `import imp` to a
    # sibling checkout.
    _make_imp_checkout(tmp_path)
    env = engine_env(project_root=tmp_path, quest_id="2026-06-04-eee555",
                     quest_title="t")
    assert env["PYTHONPATH"] == str(tmp_path / "src")


def test_engine_env_omits_pythonpath_without_src_layout(tmp_path: Path) -> None:
    env = engine_env(project_root=tmp_path, quest_id="2026-06-04-eee555",
                     quest_title="t")
    assert "PYTHONPATH" not in env


def test_engine_env_omits_pythonpath_for_non_imp_src_layout(tmp_path: Path) -> None:
    # A bare src/ dir, or a non-imp src-layout project, must NOT get its src/
    # forced onto subprocess PYTHONPATH — the shadow is imp-on-imp only and must
    # never perturb sys.path for unrelated projects' gate/agent subprocesses.
    (tmp_path / "src" / "otherpkg").mkdir(parents=True)
    (tmp_path / "src" / "otherpkg" / "__init__.py").write_text("")
    env = engine_env(project_root=tmp_path, quest_id="2026-06-04-eee555",
                     quest_title="t")
    assert "PYTHONPATH" not in env


def test_overlay_prepends_worktree_src_to_inherited_pythonpath(
    tmp_path: Path,
) -> None:
    _make_imp_checkout(tmp_path)
    parent = {"PYTHONPATH": "/tmp/ambient"}
    overlay = engine_env(project_root=tmp_path, quest_id="2026-06-04-eee555",
                     quest_title="t")
    env = overlay_engine_env(parent, overlay)
    # worktree src first (wins over the editable .pth), ambient preserved after.
    assert env["PYTHONPATH"] == f"{tmp_path / 'src'}{os.pathsep}/tmp/ambient"


def test_overlay_sets_pythonpath_alone_when_none_inherited(tmp_path: Path) -> None:
    _make_imp_checkout(tmp_path)
    overlay = engine_env(project_root=tmp_path, quest_id="2026-06-04-eee555",
                     quest_title="t")
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
    overlay = engine_env(project_root=tmp_path, quest_id="2026-06-04-eee555",
                     quest_title="t")
    env = overlay_engine_env(parent, overlay)
    assert env["PYTHONPATH"] == f"{src}{os.pathsep}/tmp/ambient"
    assert env["PYTHONPATH"].count(src) == 1


def test_overlay_strips_inherited_quest_dir_when_unset() -> None:
    stale = {"IMP_QUEST_DIR": "/old/home", "OTHER": "x"}
    result = overlay_engine_env(
        stale,
        engine_env(project_root=Path("/wt"), quest_id="2026-06-04-eee555",
                   quest_title="t"),
    )
    assert "IMP_QUEST_DIR" not in result


def test_engine_env_includes_quest_dir_when_given() -> None:
    env = engine_env(project_root=Path("/wt"), quest_id="2026-06-04-eee555",
                     quest_title="t",
                     quest_dir=Path("/ctl/.imp/quests/2026-06-04-eee555"))
    assert env["IMP_QUEST_DIR"] == "/ctl/.imp/quests/2026-06-04-eee555"


def test_engine_env_omits_quest_dir_when_absent() -> None:
    env = engine_env(project_root=Path("/wt"), quest_id="2026-06-04-eee555",
                     quest_title="t")
    assert "IMP_QUEST_DIR" not in env


def test_quest_dir_is_engine_managed() -> None:
    from imp.engine.env import ENGINE_ENV_VARS
    assert "IMP_QUEST_DIR" in ENGINE_ENV_VARS


def test_engine_env_injects_test_cmd_fast_when_set() -> None:
    env = engine_env(project_root=Path("/wt"), quest_id="c", quest_title="t",
                     test_gates=TestGates(fast="make check"))
    assert env["IMP_TEST_CMD_FAST"] == "make check"


def test_engine_env_injects_test_cmd_slow_when_set() -> None:
    env = engine_env(project_root=Path("/wt"), quest_id="c", quest_title="t",
                     test_gates=TestGates(slow="make check && make test-live"))
    assert env["IMP_TEST_CMD_SLOW"] == "make check && make test-live"


def test_engine_env_injects_test_cmd_timeouts_as_strings() -> None:
    env = engine_env(project_root=Path("/wt"), quest_id="c", quest_title="t",
                     test_gates=TestGates(fast_timeout=120, slow_timeout=300))
    assert env["IMP_TEST_CMD_FAST_TIMEOUT"] == "120"   # run-verify.sh reads a string
    assert env["IMP_TEST_CMD_SLOW_TIMEOUT"] == "300"


def test_engine_env_omits_test_cmd_vars_without_test_gates() -> None:
    env = engine_env(project_root=Path("/wt"), quest_id="c", quest_title="t")
    for v in ("IMP_TEST_CMD_FAST", "IMP_TEST_CMD_SLOW",
              "IMP_TEST_CMD_FAST_TIMEOUT", "IMP_TEST_CMD_SLOW_TIMEOUT"):
        assert v not in env


def test_engine_env_omits_test_cmd_fast_when_empty() -> None:
    # Falsy command (opted-out "") is treated as absent => var omitted.
    env = engine_env(project_root=Path("/wt"), quest_id="c", quest_title="t",
                     test_gates=TestGates(fast=""))
    assert "IMP_TEST_CMD_FAST" not in env


def test_overlay_strips_inherited_test_cmd_fast_when_unset() -> None:
    # A stale ambient IMP_TEST_CMD_FAST (imp-on-imp) must not leak past an unset
    # bundle — engine_env is authoritative; overlay strips the inherited copy.
    parent = {"IMP_TEST_CMD_FAST": "make stale", "FOO": "bar"}
    overlay = engine_env(project_root=Path("/wt"), quest_id="c", quest_title="t")
    env = overlay_engine_env(parent, overlay)
    assert "IMP_TEST_CMD_FAST" not in env
    assert env["FOO"] == "bar"


def test_overlay_configured_test_cmd_fast_overrides_inherited() -> None:
    parent = {"IMP_TEST_CMD_FAST": "make stale"}
    overlay = engine_env(project_root=Path("/wt"), quest_id="c", quest_title="t",
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
    env = engine_env(project_root=Path("/wt"), quest_id="c", quest_title="t",
                     test_gates=TestGates(fast_timeout=0, slow_timeout=0))
    assert env["IMP_TEST_CMD_FAST_TIMEOUT"] == "0"
    assert env["IMP_TEST_CMD_SLOW_TIMEOUT"] == "0"


def test_engine_env_omits_test_cmd_slow_when_empty() -> None:
    env = engine_env(project_root=Path("/wt"), quest_id="c", quest_title="t",
                     test_gates=TestGates(slow=""))
    assert "IMP_TEST_CMD_SLOW" not in env


def test_overlay_strips_all_inherited_test_cmd_vars_when_unset() -> None:
    parent = {"IMP_TEST_CMD_FAST": "make stale", "IMP_TEST_CMD_SLOW": "make stale",
              "IMP_TEST_CMD_FAST_TIMEOUT": "999", "IMP_TEST_CMD_SLOW_TIMEOUT": "999"}
    overlay = engine_env(project_root=Path("/wt"), quest_id="c", quest_title="t")
    env = overlay_engine_env(parent, overlay)
    for v in ("IMP_TEST_CMD_FAST", "IMP_TEST_CMD_SLOW",
              "IMP_TEST_CMD_FAST_TIMEOUT", "IMP_TEST_CMD_SLOW_TIMEOUT"):
        assert v not in env


def test_engine_env_injects_output_path_when_given() -> None:
    env = engine_env(project_root=Path("/wt"), quest_id="c", quest_title="t",
                     output_path=Path("/tmp/imp-output-abc.json"))
    assert env["IMP_OUTPUT"] == "/tmp/imp-output-abc.json"


def test_engine_env_omits_output_path_when_absent() -> None:
    env = engine_env(project_root=Path("/wt"), quest_id="c", quest_title="t")
    assert "IMP_OUTPUT" not in env


def test_output_path_var_is_engine_managed() -> None:
    from imp.engine.env import ENGINE_ENV_VARS
    assert "IMP_OUTPUT" in ENGINE_ENV_VARS


def test_engine_env_emits_headless_vars_unconditionally() -> None:
    # Emitted for every subprocess with no gates/dir/output — the four headless
    # vars are unconditional, unlike IMP_TEST_CMD_* / IMP_QUEST_DIR.
    env = engine_env(project_root=Path("/wt"), quest_id="c", quest_title="t")
    assert env["GIT_EDITOR"] == "true"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["EDITOR"] == "true"
    assert env["VISUAL"] == "true"


def test_headless_vars_are_engine_managed() -> None:
    from imp.engine.env import ENGINE_ENV_VARS
    for v in ("GIT_EDITOR", "GIT_TERMINAL_PROMPT", "EDITOR", "VISUAL"):
        assert v in ENGINE_ENV_VARS


def test_overlay_strips_ambient_interactive_editor() -> None:
    # A user's ambient EDITOR=vim / GIT_EDITOR=nano is exactly the hang hazard;
    # engine values are authoritative, so overlay drops the ambient copy.
    parent = {"EDITOR": "vim", "VISUAL": "vim", "GIT_EDITOR": "nano",
              "GIT_TERMINAL_PROMPT": "1", "FOO": "bar"}
    env = overlay_engine_env(
        parent,
        engine_env(project_root=Path("/wt"), quest_id="c", quest_title="t"),
    )
    assert env["EDITOR"] == "true"
    assert env["VISUAL"] == "true"
    assert env["GIT_EDITOR"] == "true"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["FOO"] == "bar"   # untouched non-engine vars pass through

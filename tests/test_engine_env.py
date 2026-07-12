import os
from pathlib import Path

from imp.engine.env import TestGates, engine_env, overlay_engine_env


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


def test_engine_env_pythonpath_single_root_resolves_against_worktree() -> None:
    env = engine_env(project_root=Path("/wt"), quest_id="c", quest_title="t",
                     pythonpath=("src",))
    assert env["PYTHONPATH"] == "/wt/src"


def test_engine_env_pythonpath_dot_is_worktree_root() -> None:
    env = engine_env(project_root=Path("/wt"), quest_id="c", quest_title="t",
                     pythonpath=(".",))
    assert env["PYTHONPATH"] == "/wt"


def test_engine_env_pythonpath_multi_root_joined_in_listed_order() -> None:
    env = engine_env(project_root=Path("/wt"), quest_id="c", quest_title="t",
                     pythonpath=("src", "vendor"))
    assert env["PYTHONPATH"] == f"/wt/src{os.pathsep}/wt/vendor"


def test_engine_env_omits_pythonpath_when_empty() -> None:
    env = engine_env(project_root=Path("/wt"), quest_id="c", quest_title="t")
    assert "PYTHONPATH" not in env


def test_overlay_prepends_pythonpath_prefix_to_inherited() -> None:
    parent = {"PYTHONPATH": "/tmp/ambient"}
    overlay = engine_env(project_root=Path("/wt"), quest_id="c", quest_title="t",
                         pythonpath=("src",))
    env = overlay_engine_env(parent, overlay)
    assert env["PYTHONPATH"] == f"/wt/src{os.pathsep}/tmp/ambient"


def test_overlay_sets_pythonpath_alone_when_none_inherited() -> None:
    overlay = engine_env(project_root=Path("/wt"), quest_id="c", quest_title="t",
                         pythonpath=("src",))
    env = overlay_engine_env({}, overlay)
    assert env["PYTHONPATH"] == "/wt/src"


def test_overlay_per_entry_dedups_multi_root_prefix() -> None:
    # Per-entry dedup: an inherited entry equal to ANY prefix entry is dropped,
    # empty entries dropped, surviving inherited entries kept in order after the
    # whole prefix. (Whole-string dedup would leave /wt/vendor duplicated.)
    parent = {"PYTHONPATH": f"/wt/vendor{os.pathsep}{os.pathsep}/tmp/ambient"}
    overlay = engine_env(project_root=Path("/wt"), quest_id="c", quest_title="t",
                         pythonpath=("src", "vendor"))
    env = overlay_engine_env(parent, overlay)
    assert env["PYTHONPATH"] == (
        f"/wt/src{os.pathsep}/wt/vendor{os.pathsep}/tmp/ambient")


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

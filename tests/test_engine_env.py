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

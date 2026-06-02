from pathlib import Path

from imp.engine.env import engine_env


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

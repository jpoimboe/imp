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

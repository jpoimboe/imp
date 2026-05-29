from __future__ import annotations

from pathlib import Path

from imp.engine.env import engine_env


def test_engine_env_returns_both_vars_as_strings() -> None:
    env = engine_env(
        project_root=Path("/home/u/proj"),
        artifacts_dir=Path("/home/u/proj/.imp/changes/1/artifacts"),
    )
    assert env == {
        "IMP_PROJECT_ROOT": "/home/u/proj",
        "IMP_ARTIFACTS_DIR": "/home/u/proj/.imp/changes/1/artifacts",
    }
    assert all(isinstance(v, str) for v in env.values())

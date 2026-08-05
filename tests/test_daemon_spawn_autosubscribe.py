from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from imp.remote.daemon import spawn
from tests.conftest import _seed_control


def test_create_quest_sets_autosubscribe_env_on_the_resume_it_spawns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    captured: dict[str, Any] = {}

    def fake_spawn(argv: list[str], env: Any, cwd: Path) -> None:
        captured["env"] = dict(env)
        captured["argv"] = argv

    monkeypatch.setattr(spawn, "_spawn_detached", fake_spawn)

    quest_id = spawn.create_quest(tmp_path, "add dark mode")

    assert captured["env"]["IMP_REMOTE_AUTOSUBSCRIBE"] == "1"
    assert captured["argv"] == [
        sys.executable, "-m", "imp", "resume", quest_id, "--headless"]

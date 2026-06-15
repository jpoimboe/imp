from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from imp.remote.daemon import spawn


def test_create_change_sets_autosubscribe_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_spawn(argv: list, env: Any, cwd: Path) -> None:
        captured["env"] = dict(env)
        captured["argv"] = argv

    monkeypatch.setattr(spawn, "_spawn_detached", fake_spawn)
    spawn.create_change(tmp_path, "add dark mode")
    assert captured["env"]["IMP_REMOTE_AUTOSUBSCRIBE"] == "1"
    assert "new" in captured["argv"]


def test_resume_change_does_not_set_autosubscribe_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_spawn(argv: list, env: Any, cwd: Path) -> None:
        captured["env"] = dict(env)

    monkeypatch.setattr(spawn, "_spawn_detached", fake_spawn)
    spawn.resume_change(tmp_path, "2026-06-14-abc", "approved")
    assert "IMP_REMOTE_AUTOSUBSCRIBE" not in captured["env"]

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from imp.remote.daemon import spawn


def _capture(monkeypatch: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake(argv: list[str], env: dict[str, str], cwd: Path) -> None:
        calls.append({"argv": argv, "env": env, "cwd": cwd})

    monkeypatch.setattr(spawn, "_spawn_detached", fake)
    return calls


def test_resume_change_runs_imp_resume_with_message(monkeypatch: Any, tmp_path: Path) -> None:
    calls = _capture(monkeypatch)
    spawn.resume_change(tmp_path, "2026-06-13-abc", "yes do it")
    assert calls[0]["argv"] == [
        sys.executable, "-m", "imp", "resume", "2026-06-13-abc",
        "--non-interactive", "--message", "yes do it"]
    assert calls[0]["cwd"] == tmp_path


def test_create_change_spawns_imp_new(monkeypatch: Any, tmp_path: Path) -> None:
    calls = _capture(monkeypatch)
    spawn.create_change(tmp_path, "add dark mode")
    assert calls[0]["argv"] == [
        sys.executable, "-m", "imp", "new", "--non-interactive",
        "--message", "add dark mode"]
    assert "IMP_REMOTE_CONTROL_SOCK" not in calls[0]["env"]


def test_abandon_change_runs_imp_abandon_yes(monkeypatch: Any, tmp_path: Path) -> None:
    calls = _capture(monkeypatch)
    spawn.abandon_change(tmp_path, "2026-06-13-abc")
    assert calls[0]["argv"] == [
        sys.executable, "-m", "imp", "abandon", "2026-06-13-abc", "--yes"]


def test_spawn_env_strips_engine_managed_vars(monkeypatch: Any, tmp_path: Path) -> None:
    calls = _capture(monkeypatch)
    monkeypatch.setenv("IMP_LINT_COMMAND", "stale-lint")
    spawn.resume_change(tmp_path, "2026-06-13-abc", "go")
    # A stale engine var from an outer imp must not leak into the spawned imp.
    assert "IMP_LINT_COMMAND" not in calls[0]["env"]

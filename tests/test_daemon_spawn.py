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


def test_create_quest_spawns_imp_new(monkeypatch: Any, tmp_path: Path) -> None:
    calls = _capture(monkeypatch)
    spawn.create_quest(tmp_path, "add dark mode")
    assert calls[0]["argv"] == [
        sys.executable, "-m", "imp", "new", "--non-interactive",
        "--message", "add dark mode"]
    assert "IMP_REMOTE_CONTROL_SOCK" not in calls[0]["env"]


def test_resume_change_spawns_imp_resume_for_a_unique_match(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    calls = _capture(monkeypatch)
    monkeypatch.setattr(
        "imp.cli.main._iter_quest_ids", lambda _c: ["2026-06-13-aaaaaa"])
    msg = spawn.resume_change(tmp_path, "aaaa")
    assert calls[0]["argv"] == [
        sys.executable, "-m", "imp", "resume", "2026-06-13-aaaaaa",
        "--non-interactive"]
    assert calls[0]["env"]["IMP_REMOTE_AUTOSUBSCRIBE"] == "1"
    assert "2026-06-13-aaaaaa" in msg


def test_resume_change_reports_ambiguous_and_does_not_spawn(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    calls = _capture(monkeypatch)
    monkeypatch.setattr(
        "imp.cli.main._iter_quest_ids",
        lambda _c: ["2026-06-13-aaaaaa", "2026-06-13-aaabbb"])
    msg = spawn.resume_change(tmp_path, "aaa")
    assert calls == []
    assert "ambiguous" in msg
    assert "'aaa'" in msg


def test_resume_change_reports_no_match_and_does_not_spawn(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    calls = _capture(monkeypatch)
    monkeypatch.setattr("imp.cli.main._iter_quest_ids", lambda _c: [])
    msg = spawn.resume_change(tmp_path, "ghost")
    assert calls == []
    assert "'ghost'" in msg


def test_abandon_change_runs_imp_abandon_and_returns_rc(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    def fake(argv: list[str], env: dict[str, str], cwd: Path) -> int:
        captured["argv"] = argv
        captured["env"] = env
        captured["cwd"] = cwd
        return 2

    monkeypatch.setattr(spawn, "_run_foreground", fake)
    rc = spawn.abandon_change(tmp_path, "2026-06-13-abc")
    assert rc == 2
    assert captured["argv"] == [
        sys.executable, "-m", "imp", "abandon", "2026-06-13-abc"]
    assert captured["cwd"] == tmp_path
    assert "IMP_REMOTE_CONTROL_SOCK" not in captured["env"]


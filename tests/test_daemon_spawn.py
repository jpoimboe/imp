from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from imp.cli.main import BaseResolutionError
from imp.paths import project_state_dir
from imp.remote.daemon import spawn
from tests.conftest import _seed_control


def _capture(monkeypatch: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake(argv: list[str], env: dict[str, str], cwd: Path) -> None:
        calls.append({"argv": argv, "env": env, "cwd": cwd})

    monkeypatch.setattr(spawn, "_spawn_detached", fake)
    return calls


def test_create_quest_writes_the_draft_before_spawning_an_interactive_resume(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    _seed_control(tmp_path)
    seen: list[dict[str, Any]] = []

    # Reading quest.json from inside the spawn stub is the assertion that the
    # draft is on disk *before* the resume is detached, not merely afterwards.
    def fake(argv: list[str], env: dict[str, str], cwd: Path) -> None:
        spawned_id = argv[argv.index("resume") + 1]
        meta = json.loads(
            (project_state_dir(tmp_path) / "quests" / spawned_id
             / "quest.json").read_text())
        seen.append({"argv": argv, "env": env, "cwd": cwd, "meta": meta})

    monkeypatch.setattr(spawn, "_spawn_detached", fake)

    quest_id = spawn.create_quest(tmp_path, "add dark mode")

    assert seen[0]["argv"] == [sys.executable, "-m", "imp", "resume", quest_id]
    assert seen[0]["env"]["IMP_REMOTE_AUTOSUBSCRIBE"] == "1"
    assert "IMP_REMOTE_CONTROL_SOCK" not in seen[0]["env"]
    assert seen[0]["cwd"] == tmp_path
    assert seen[0]["meta"]["seed_opening"] == "add dark mode"


def test_create_quest_raises_and_spawns_nothing_when_the_base_is_unresolvable(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    _seed_control(tmp_path)
    (tmp_path / "imp" / "config.yaml").write_text("default-workflow: imp\n")
    calls = _capture(monkeypatch)

    with pytest.raises(BaseResolutionError):
        spawn.create_quest(tmp_path, "add dark mode")

    assert calls == []
    assert not (project_state_dir(tmp_path) / "quests").exists()


def test_resume_quest_spawns_imp_resume_for_a_unique_match(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    calls = _capture(monkeypatch)
    monkeypatch.setattr(
        "imp.cli.main._live_quest_ids", lambda _c: ["2026-06-13-aaaaaa"])
    msg = spawn.resume_quest(tmp_path, "aaaa")
    assert calls[0]["argv"] == [
        sys.executable, "-m", "imp", "resume", "2026-06-13-aaaaaa"]
    assert calls[0]["env"]["IMP_REMOTE_AUTOSUBSCRIBE"] == "1"
    assert "2026-06-13-aaaaaa" in msg


def test_resume_quest_reports_ambiguous_and_does_not_spawn(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    calls = _capture(monkeypatch)
    monkeypatch.setattr(
        "imp.cli.main._live_quest_ids",
        lambda _c: ["2026-06-13-aaaaaa", "2026-06-13-aaabbb"])
    msg = spawn.resume_quest(tmp_path, "aaa")
    assert calls == []
    assert "ambiguous" in msg
    assert "'aaa'" in msg


def test_resume_quest_reports_no_match_and_does_not_spawn(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    calls = _capture(monkeypatch)
    monkeypatch.setattr("imp.cli.main._live_quest_ids", lambda _c: [])
    msg = spawn.resume_quest(tmp_path, "ghost")
    assert calls == []
    assert "'ghost'" in msg


@pytest.mark.parametrize(("autonomous", "flags"), [
    (False, []),
    (True, ["--autonomous"]),
])
def test_start_quest_spawns_a_resume_carrying_only_the_given_flags(
    monkeypatch: Any, tmp_path: Path, autonomous: bool, flags: list[str],
) -> None:
    calls = _capture(monkeypatch)
    spawn.start_quest(tmp_path, "2026-06-13-aaaaaa", autonomous=autonomous)
    assert calls[0]["argv"] == [
        sys.executable, "-m", "imp", "resume", "2026-06-13-aaaaaa", *flags]
    assert calls[0]["cwd"] == tmp_path
    assert "IMP_REMOTE_CONTROL_SOCK" not in calls[0]["env"]
    assert "IMP_REMOTE_AUTOSUBSCRIBE" not in calls[0]["env"]

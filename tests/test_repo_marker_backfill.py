"""The startup migration that backfills the `repo` ownership marker into imp-owned
state directories, so `_checked_override` has an input to refuse a foreign checkout with."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from imp.cli.config_migrate import _state_candidates
from imp.paths import imp_home
from tests.conftest import _init_repo

# Every fixture builds real git checkouts; git spawns are slow under a saturated -n auto.
pytestmark = pytest.mark.timeout(240)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args],
                   check=True, capture_output=True, text=True)


def _checkout(parent: Path, name: str) -> Path:
    """A real git checkout — find_control_root only accepts a genuine work tree."""
    root = parent / name
    root.mkdir(parents=True)
    _init_repo(root)
    return root.resolve()


def _state_dir(name: str) -> Path:
    directory = imp_home() / "projects" / name
    directory.mkdir(parents=True)
    return directory


def _quest(state_dir: Path, quest_id: str, project_root: Path | str) -> None:
    home = state_dir / "quests" / quest_id
    home.mkdir(parents=True)
    (home / "quest.json").write_text(json.dumps(
        {"id": quest_id, "title": "t", "base_branch": "main",
         "project_root": str(project_root)}, indent=2, sort_keys=True))


def _record(state_dir: Path, quest_id: str, project_root: Path | str) -> None:
    records = state_dir / "archive" / "records"
    records.mkdir(parents=True, exist_ok=True)
    (records / f"{quest_id}.json").write_text(json.dumps(
        {"id": quest_id, "title": "t",
         "quest_meta": {"id": quest_id, "project_root": str(project_root)}},
        indent=2, sort_keys=True))


def _worktree(state_dir: Path, control: Path, quest_id: str) -> Path:
    path = state_dir / "worktrees" / quest_id
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(control, "worktree", "add", "-q", "-b", f"imp/{quest_id}", str(path), "main")
    return path


def test_candidates_come_from_quest_homes_and_archive_records(tmp_path: Path) -> None:
    """Spec AC 6's source: a project between quests has an archive and no live home."""
    state = _state_dir("widget")
    _quest(state, "aaa111", tmp_path / "from-quest")
    _record(state, "bbb222", tmp_path / "from-archive")

    assert sorted(_state_candidates(state)) == [tmp_path / "from-archive",
                                                tmp_path / "from-quest"]


def test_a_directory_with_no_records_yields_no_candidates() -> None:
    assert _state_candidates(_state_dir("widget")) == []


def test_a_record_without_a_project_root_yields_no_candidate() -> None:
    state = _state_dir("widget")
    (state / "quests" / "aaa111").mkdir(parents=True)
    (state / "quests" / "aaa111" / "quest.json").write_text('{"id": "aaa111"}')

    assert _state_candidates(state) == []


def test_a_malformed_record_raises_for_the_sweep_to_isolate(tmp_path: Path) -> None:
    """Malformed records make the whole DIRECTORY undecidable, so the error is raised
    here and caught per-directory by the sweep (spec AC 8) — never silently ignored."""
    state = _state_dir("widget")
    _quest(state, "aaa111", tmp_path / "somewhere")
    (state / "quests" / "aaa111" / "quest.json").write_text("{not json")

    with pytest.raises(json.JSONDecodeError):
        _state_candidates(state)

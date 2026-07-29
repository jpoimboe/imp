"""The startup migration that moves <control_root>/.imp into the project's state
directory. Its input IS the old layout, so this file constructs `<root>/.imp`
paths directly — the exemption spec AC 44 names."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from imp.cli import state_migrate
from imp.cli.state_migrate import _move_entries


def _seed(legacy: Path) -> None:
    """The four top-level entries a real pre-migration `.imp` carries."""
    (legacy / "quests" / "aaa111").mkdir(parents=True)
    (legacy / "quests" / "aaa111" / "quest.json").write_text('{"id": "aaa111"}')
    (legacy / "worktrees" / "aaa111").mkdir(parents=True)
    (legacy / "worktrees" / "aaa111" / "README.md").write_text("seed\n")
    (legacy / "archive" / "records").mkdir(parents=True)
    (legacy / "config.yaml").write_text("providers: [anthropic]\n")


def test_move_entries_relocates_every_top_level_entry(tmp_path: Path) -> None:
    legacy = tmp_path / "proj" / ".imp"
    legacy.mkdir(parents=True)
    _seed(legacy)
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    _move_entries(legacy, state_dir)

    assert (state_dir / "quests" / "aaa111" / "quest.json").read_text() == '{"id": "aaa111"}'
    assert (state_dir / "worktrees" / "aaa111" / "README.md").read_text() == "seed\n"
    assert (state_dir / "archive" / "records").is_dir()
    assert (state_dir / "config.yaml").read_text() == "providers: [anthropic]\n"
    # The emptied directory itself is the migration's in-progress marker; the
    # move protocol never removes it.
    assert list(legacy.iterdir()) == []


def test_move_entries_keeps_a_destination_a_previous_run_already_wrote(
    tmp_path: Path,
) -> None:
    """Resumption: the destination is authoritative once it exists, and the
    leftover source is dropped rather than merged."""
    legacy = tmp_path / "proj" / ".imp"
    legacy.mkdir(parents=True)
    _seed(legacy)
    state_dir = tmp_path / "state"
    (state_dir / "quests" / "aaa111").mkdir(parents=True)
    (state_dir / "quests" / "aaa111" / "quest.json").write_text('{"id": "moved"}')

    _move_entries(legacy, state_dir)

    assert (state_dir / "quests" / "aaa111" / "quest.json").read_text() == '{"id": "moved"}'
    assert not (legacy / "quests").exists()


def test_move_entries_discards_a_stale_staging_dir_from_an_interrupted_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = tmp_path / "proj" / ".imp"
    legacy.mkdir(parents=True)
    _seed(legacy)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / ".migrating-quests").mkdir()
    (state_dir / ".migrating-quests" / "half.txt").write_text("partial copy")
    monkeypatch.setattr(state_migrate, "_same_device", lambda a, b: False)

    _move_entries(legacy, state_dir)

    assert (state_dir / "quests" / "aaa111" / "quest.json").read_text() == '{"id": "aaa111"}'
    assert not (state_dir / "quests" / "half.txt").exists()
    assert not (state_dir / ".migrating-quests").exists()


def test_cross_device_copy_keeps_symlinks_as_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The quest-home symlink inside each worktree must survive the copy as a
    (stale) link — resolving it would duplicate the whole quest home."""
    legacy = tmp_path / "proj" / ".imp"
    legacy.mkdir(parents=True)
    _seed(legacy)
    link = legacy / "worktrees" / "aaa111" / ".imp" / "quests" / "aaa111"
    link.parent.mkdir(parents=True)
    link.symlink_to(legacy / "quests" / "aaa111")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(state_migrate, "_same_device", lambda a, b: False)

    _move_entries(legacy, state_dir)

    moved = state_dir / "worktrees" / "aaa111" / ".imp" / "quests" / "aaa111"
    assert moved.is_symlink()
    assert os.readlink(moved) == str(legacy / "quests" / "aaa111")
    assert not (legacy / "worktrees").exists()


def test_same_device_helper_reports_one_filesystem(tmp_path: Path) -> None:
    """The seam the cross-device tests monkeypatch: two dirs on one tmpfs match."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert state_migrate._same_device(a, b) is True

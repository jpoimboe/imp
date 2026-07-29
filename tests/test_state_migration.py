"""The startup migration that moves <control_root>/.imp into the project's state
directory. Its input IS the old layout, so this file constructs `<root>/.imp`
paths directly — the exemption spec AC 44 names."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from imp.cli import state_migrate
from imp.cli.state_migrate import (
    _move_entries,
    _relink_quest_homes,
    _repair_worktrees,
    _rewrite_archive_roots,
    _rewrite_quest_roots,
)
from imp.engine.quest import link_quest_home
from tests.conftest import _init_repo


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


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(root), *args],
                          check=check, capture_output=True, text=True)


def _repo(root: Path) -> Path:
    """A committed checkout whose state still sits at <root>/.imp."""
    root.mkdir(parents=True, exist_ok=True)
    _init_repo(root)
    (root / ".imp").mkdir()
    # Resolved: find_control_root resolves, and the pin assertions compare against it.
    return root.resolve()


def _meta(state_dir: Path, quest_id: str) -> dict[str, object]:
    return json.loads((state_dir / "quests" / quest_id / "quest.json").read_text())


def test_quest_roots_are_prefix_substituted_and_the_draft_is_left_alone(
    tmp_path: Path,
) -> None:
    """A materialized quest's project_root names its worktree and moves with it; a
    draft's names the control root and must not be rewritten (spec AC 17a, 17c)."""
    control = tmp_path / "proj"
    legacy = control / ".imp"
    state_dir = tmp_path / "state"
    for quest_id, root, live in (("mig111", legacy / "worktrees" / "mig111", True),
                                 ("drf111", control, False)):
        home = state_dir / "quests" / quest_id
        home.mkdir(parents=True)
        (home / "quest.json").write_text(json.dumps(
            {"id": quest_id, "title": "t", "project_root": str(root),
             "materialized": live}, indent=2, sort_keys=True))

    _rewrite_quest_roots(legacy, state_dir)

    assert _meta(state_dir, "mig111")["project_root"] == str(
        state_dir / "worktrees" / "mig111")
    assert _meta(state_dir, "mig111")["title"] == "t"       # untouched keys survive
    assert _meta(state_dir, "drf111")["project_root"] == str(control)


def test_archive_records_carry_the_new_worktree_path(tmp_path: Path) -> None:
    """The embedded quest_meta.project_root moves too (spec AC 17b)."""
    control = tmp_path / "proj"
    legacy = control / ".imp"
    state_dir = tmp_path / "state"
    records = state_dir / "archive" / "records"
    records.mkdir(parents=True)
    (records / "arc111.json").write_text(json.dumps({
        "id": "arc111", "title": "old",
        "quest_meta": {"id": "arc111",
                       "project_root": str(legacy / "worktrees" / "arc111")},
    }, indent=2, sort_keys=True))

    _rewrite_archive_roots(legacy, state_dir)

    rec = json.loads((records / "arc111.json").read_text())
    assert rec["quest_meta"]["project_root"] == str(state_dir / "worktrees" / "arc111")
    assert rec["title"] == "old"


@pytest.mark.timeout(240)
def test_repair_and_relink_restore_a_moved_worktree(tmp_path: Path) -> None:
    """git's admin gitdir file and the quest-home symlink are both absolute and
    both stale after the move (spec AC 15, 16). A spike worktree carries the same
    link under a directory name that is not a quest id, and is relinked too."""
    control = _repo(tmp_path / "proj")
    legacy = control / ".imp"
    worktree = legacy / "worktrees" / "aaa111"
    spike = legacy / "worktrees" / "spike-aaa111-1"
    _git(control, "worktree", "add", "-q", "-b", "imp/aaa111", str(worktree), "main")
    _git(control, "worktree", "add", "-q", "-b", "imp/spike", str(spike), "main")
    home = legacy / "quests" / "aaa111"
    home.mkdir(parents=True)
    (home / "quest.json").write_text('{"id": "aaa111"}')
    link_quest_home(worktree, home)
    link_quest_home(spike, home)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _move_entries(legacy, state_dir)

    _repair_worktrees(control, legacy, state_dir)
    _relink_quest_homes(legacy, state_dir)

    moved = state_dir / "worktrees" / "aaa111"
    assert _git(moved, "status", "--porcelain", check=False).returncode == 0
    assert str(moved) in _git(control, "worktree", "list").stdout
    assert (moved / ".imp" / "quests" / "aaa111").resolve() == (
        state_dir / "quests" / "aaa111")
    assert (state_dir / "worktrees" / "spike-aaa111-1" / ".imp" / "quests" / "aaa111"
            ).resolve() == state_dir / "quests" / "aaa111"


@pytest.mark.timeout(240)
def test_repair_ignores_a_directory_git_never_registered(tmp_path: Path) -> None:
    """`git worktree repair` exits 1 on a path git has no record of, and imp's own
    force-teardown paths leave such directories behind. By repair time the state has
    already moved and the legacy directory is removed only at the very end, so
    treating one as fatal would re-fail identically on every later invocation — the
    repair set therefore comes from git, not from iterdir()."""
    control = _repo(tmp_path / "proj")
    legacy = control / ".imp"
    worktree = legacy / "worktrees" / "aaa111"
    _git(control, "worktree", "add", "-q", "-b", "imp/aaa111", str(worktree), "main")
    (legacy / "worktrees" / "leftover").mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _move_entries(legacy, state_dir)

    _repair_worktrees(control, legacy, state_dir)      # must not raise

    moved = state_dir / "worktrees" / "aaa111"
    assert str(moved) in _git(control, "worktree", "list").stdout
    assert (state_dir / "worktrees" / "leftover").is_dir()


@pytest.mark.timeout(240)
def test_a_repair_git_refuses_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Same reasoning for a repair that fails on a registered worktree: report
    git's own words and let the migration finish."""
    control = _repo(tmp_path / "proj")
    legacy = control / ".imp"
    worktree = legacy / "worktrees" / "aaa111"
    _git(control, "worktree", "add", "-q", "-b", "imp/aaa111", str(worktree), "main")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _move_entries(legacy, state_dir)
    real_run = subprocess.run

    def refuse(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "repair" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "fatal: cannot repair\n")
        return real_run(argv, **kwargs)     # type: ignore[call-overload,no-any-return]

    monkeypatch.setattr(state_migrate.subprocess, "run", refuse)

    _repair_worktrees(control, legacy, state_dir)      # must not raise

    assert capsys.readouterr().err == (
        "imp: git could not repair every moved worktree:\nfatal: cannot repair\n")

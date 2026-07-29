"""The startup migration that moves <control_root>/.imp into the project's state
directory. Its input IS the old layout, so this file constructs `<root>/.imp`
paths directly — the exemption spec AC 44 names."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple

import pytest

from imp.cli import state_migrate
from imp.cli.state_migrate import (
    _move_entries,
    _relink_quest_homes,
    _repair_worktrees,
    _rewrite_archive_roots,
    _rewrite_quest_roots,
    _target_control_root,
    migrate_project_state,
)
from imp.engine import archive
from imp.engine.archive.record import read_record
from imp.engine.quest import Quest, link_quest_home, live_quest_ids
from imp.paths import display_path, imp_home, project_state_dir
from tests.conftest import _assert_outside_a_git_work_tree, _init_repo, _seed_control


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


DEFERRED_NOTICE = ("imp: not moving .imp/ yet — a quest is running; "
                   "re-run when it finishes\n")


def _explode(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("unexpected subprocess spawn")


def _dead_pid() -> int:
    # A reaped child's pid is gone (and not reused within this test).
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _live_quest(legacy: Path, quest_id: str) -> Path:
    home = legacy / "quests" / quest_id
    home.mkdir(parents=True)
    (home / "quest.json").write_text(json.dumps(
        {"id": quest_id, "title": "live", "base_branch": "main"}))
    return home


def test_an_already_migrated_checkout_spawns_no_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The permanent steady state: the pure-filesystem pre-check must decline
    before the control root is ever resolved (spec AC 22)."""
    _assert_outside_a_git_work_tree(tmp_path)
    control = _repo(tmp_path / "proj")
    shutil.rmtree(control / ".imp")
    monkeypatch.setattr(subprocess, "run", _explode)

    assert _target_control_root(control) is None


def test_an_explicit_override_is_left_alone_and_spawns_no_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec AC 23: an explicit IMP_STATE_DIR owns the path — tested first, before
    any walk or spawn, because every agent-side invocation carries one."""
    _assert_outside_a_git_work_tree(tmp_path)
    control = _repo(tmp_path / "proj")
    monkeypatch.setenv("IMP_STATE_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setattr(subprocess, "run", _explode)

    assert _target_control_root(control) is None
    assert os.environ["IMP_STATE_DIR"] == str(tmp_path / "elsewhere")
    assert (control / ".imp").is_dir()


def test_imp_s_own_home_under_a_git_home_does_not_qualify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dotfiles $HOME satisfies both the .imp and .git conjuncts; only the
    imp_home() exclusion rejects it (spec AC 24)."""
    _assert_outside_a_git_work_tree(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    _repo(home)                       # $HOME is itself a checkout...
    control = _repo(home / "proj")    # ...holding an already-migrated project
    shutil.rmtree(control / ".imp")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("IMP_HOME", str(home / ".imp"))
    monkeypatch.setattr(subprocess, "run", _explode)

    assert _target_control_root(control) is None


@pytest.mark.timeout(240)
def test_inside_a_quest_worktree_nothing_moves_and_the_old_path_is_pinned(
    tmp_path: Path,
) -> None:
    """Spec AC 25: same guard as migrate_project_config, but this one pins on the
    way out — the state it declines to move still has to be findable."""
    control = _repo(tmp_path / "proj")
    worktree = control / ".imp" / "worktrees" / "aaa111"
    _git(control, "worktree", "add", "-q", "-b", "imp/aaa111", str(worktree), "main")

    assert _target_control_root(worktree) is None
    assert os.environ["IMP_STATE_DIR"] == str(control / ".imp")
    assert worktree.is_dir()
    assert not (imp_home() / "projects").exists()      # no slug claimed


def test_a_state_dir_outside_any_git_repo_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """find_control_root raises PreconditionError here; unsuppressed, that would
    kill `imp --version` from any non-repo directory (spec AC 27)."""
    stray = tmp_path / "stray"
    (stray / ".imp").mkdir(parents=True)
    (stray / ".git").mkdir()          # satisfies the pre-check; not a real repo
    monkeypatch.chdir(stray)
    _assert_outside_a_git_work_tree(stray)

    assert _target_control_root(stray) is None
    assert "IMP_STATE_DIR" not in os.environ


def test_a_live_quest_lock_defers_the_move(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Spec AC 18: no filesystem change, the deferred line, and a pin that keeps
    the running quest visible for the rest of this invocation."""
    control = _repo(tmp_path / "proj")
    legacy = control / ".imp"
    home = _live_quest(legacy, "aaa111")
    (home / "lock").write_text(f"{os.getpid()}\n")

    assert _target_control_root(control) is None
    assert capsys.readouterr().err == DEFERRED_NOTICE
    assert os.environ["IMP_STATE_DIR"] == str(legacy)
    assert live_quest_ids(control) == ["aaa111"]       # a following `imp list` sees it
    assert not (imp_home() / "projects").exists()


def test_a_live_daemon_pid_defers_the_move(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Spec AC 19: the remote-control daemon blocks the move identically."""
    control = _repo(tmp_path / "proj")
    legacy = control / ".imp"
    (legacy / "remote-control.pid").write_text(f"{os.getpid()}\n")

    assert _target_control_root(control) is None
    assert capsys.readouterr().err == DEFERRED_NOTICE
    assert os.environ["IMP_STATE_DIR"] == str(legacy)


def test_a_stale_lock_does_not_defer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Liveness, not mere presence: a crashed run's lock file must not wedge the
    migration forever."""
    control = _repo(tmp_path / "proj")
    legacy = control / ".imp"
    home = _live_quest(legacy, "aaa111")
    (home / "lock").write_text(f"{_dead_pid()}\n")
    (legacy / "remote-control.pid").write_text(f"{_dead_pid()}\n")

    assert _target_control_root(control) == control
    assert capsys.readouterr().err == ""
    assert "IMP_STATE_DIR" not in os.environ


LIVE_ID = "mig111"
DRAFT_ID = "drf111"
ARCHIVED_ID = "arc111"


class OldLayout(NamedTuple):
    control: Path
    legacy: Path


def _seed_archive_record(control: Path, quest_id: str, project_root: Path,
                         scratch: Path) -> None:
    """One record through the real archive writer, carrying an old-layout
    project_root in its embedded quest_meta."""
    scratch.mkdir(parents=True, exist_ok=True)
    events = scratch / "events.jsonl"
    events.write_text("")
    quest_json = scratch / "quest.json"
    quest_json.write_text(json.dumps({
        "id": quest_id, "title": "archived", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": [],
        "project_root": str(project_root), "materialized": True,
    }, indent=2, sort_keys=True))
    state_json = scratch / "state.json"
    state_json.write_text(json.dumps(
        {"id": quest_id, "status": "abandoned", "step": "spec"}))
    archive.archive_quest(control, quest_id, events_path=events,
                          quest_json_path=quest_json, state_json_path=state_json)


@pytest.fixture
def old_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> OldLayout:
    """A checkout in the pre-migration shape — quest home, real linked worktree,
    un-materialized draft, archived record and local config layer all under
    <control>/.imp, written by the real production writers with the seam pinned
    there (exactly the deferral pin's own shape)."""
    (tmp_path / "proj").mkdir()
    control = (tmp_path / "proj").resolve()
    legacy = control / ".imp"
    legacy.mkdir()
    monkeypatch.setenv("IMP_STATE_DIR", str(legacy))
    _seed_control(control)
    Quest.create(control, title="live", quest_id=LIVE_ID, base_branch="main")
    Quest.create_draft(control, title="idea", quest_id=DRAFT_ID, base_branch="main")
    _seed_archive_record(control, ARCHIVED_ID, legacy / "worktrees" / ARCHIVED_ID,
                         tmp_path / "scratch")
    monkeypatch.delenv("IMP_STATE_DIR")
    return OldLayout(control, legacy)


@pytest.mark.timeout(240)
def test_migration_moves_every_entry_and_reports_it(
    old_layout: OldLayout, capsys: pytest.CaptureFixture[str],
) -> None:
    """Spec AC 14: everything moves, the checkout directory is gone, and both
    mandated lines are printed."""
    capsys.readouterr()                       # drop the fixture's own output
    migrate_project_state(old_layout.control)

    state_dir = project_state_dir(old_layout.control)
    assert (state_dir / "quests" / LIVE_ID / "quest.json").is_file()
    assert (state_dir / "quests" / DRAFT_ID / "quest.json").is_file()
    assert (state_dir / "worktrees" / LIVE_ID / "README.md").read_text() == "seed\n"
    assert (state_dir / "archive" / "records" / f"{ARCHIVED_ID}.json").is_file()
    assert (state_dir / "config.yaml").read_text().startswith("providers:")
    assert not old_layout.legacy.exists()
    # Spec's Copy section pins both lines verbatim; the paths go through
    # display_path because a tmp dir may itself live under $HOME.
    assert capsys.readouterr().err == (
        f"imp: moving .imp/ to {display_path(state_dir)} — this may take a moment\n"
        f"imp: moved .imp/ to {display_path(state_dir)}\n")


@pytest.mark.timeout(240)
def test_the_moved_worktree_still_works_and_git_knows_where_it_is(
    old_layout: OldLayout,
) -> None:
    """Spec AC 15."""
    migrate_project_state(old_layout.control)

    moved = project_state_dir(old_layout.control) / "worktrees" / LIVE_ID
    assert _git(moved, "status", "--porcelain", check=False).returncode == 0
    assert str(moved) in _git(old_layout.control, "worktree", "list").stdout


@pytest.mark.timeout(240)
def test_the_quest_home_symlink_follows_the_move(old_layout: OldLayout) -> None:
    """Spec AC 16."""
    migrate_project_state(old_layout.control)

    state_dir = project_state_dir(old_layout.control)
    link = state_dir / "worktrees" / LIVE_ID / ".imp" / "quests" / LIVE_ID
    assert link.is_symlink()
    assert link.resolve() == state_dir / "quests" / LIVE_ID


@pytest.mark.timeout(240)
def test_recorded_roots_move_except_the_draft_s(old_layout: OldLayout) -> None:
    """Spec AC 17: live quest and archived record follow the worktree; the draft's
    project_root is the control root and stays put."""
    migrate_project_state(old_layout.control)

    state_dir = project_state_dir(old_layout.control)
    assert _meta(state_dir, LIVE_ID)["project_root"] == str(
        state_dir / "worktrees" / LIVE_ID)
    assert _meta(state_dir, DRAFT_ID)["project_root"] == str(old_layout.control)
    record = read_record(state_dir / "archive" / "records", ARCHIVED_ID)
    assert record is not None
    assert record["quest_meta"]["project_root"] == str(
        state_dir / "worktrees" / ARCHIVED_ID)


@pytest.mark.timeout(240)
def test_an_interrupted_fixup_completes_on_the_next_run(
    old_layout: OldLayout, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec AC 20a: the emptied .imp survives as the in-progress marker, so the
    guard keeps passing and the next invocation finishes the job."""
    real = state_migrate._rewrite_quest_roots

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("interrupted")

    monkeypatch.setattr(state_migrate, "_rewrite_quest_roots", boom)
    with pytest.raises(OSError, match="interrupted"):
        migrate_project_state(old_layout.control)
    assert old_layout.legacy.is_dir()
    assert not (old_layout.legacy / "quests").exists()      # step 3 did complete

    monkeypatch.setattr(state_migrate, "_rewrite_quest_roots", real)
    migrate_project_state(old_layout.control)

    state_dir = project_state_dir(old_layout.control)
    assert not old_layout.legacy.exists()
    assert _meta(state_dir, LIVE_ID)["project_root"] == str(
        state_dir / "worktrees" / LIVE_ID)
    assert str(state_dir / "worktrees" / LIVE_ID) in _git(
        old_layout.control, "worktree", "list").stdout


@pytest.mark.timeout(240)
def test_an_interrupted_move_completes_on_the_next_run(
    old_layout: OldLayout, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec AC 20b: half the entries moved, the rest still at the source."""
    real_rename = os.rename

    def flaky_rename(src: Path, dst: Path) -> None:
        if dst.name == "quests":      # sorted after archive and config.yaml
            raise OSError("interrupted")
        real_rename(src, dst)

    monkeypatch.setattr(state_migrate.os, "rename", flaky_rename)
    with pytest.raises(OSError, match="interrupted"):
        migrate_project_state(old_layout.control)
    assert (old_layout.legacy / "quests").is_dir()
    assert (old_layout.legacy / "worktrees").is_dir()

    monkeypatch.setattr(state_migrate.os, "rename", real_rename)
    migrate_project_state(old_layout.control)

    state_dir = project_state_dir(old_layout.control)
    assert not old_layout.legacy.exists()
    assert (state_dir / "worktrees" / LIVE_ID / "README.md").read_text() == "seed\n"
    assert _meta(state_dir, LIVE_ID)["project_root"] == str(
        state_dir / "worktrees" / LIVE_ID)


@pytest.mark.timeout(240)
def test_the_cross_device_branch_reaches_the_same_end_state(
    old_layout: OldLayout, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec AC 21: copy/rename/delete instead of rename, same result."""
    monkeypatch.setattr(state_migrate, "_same_device", lambda a, b: False)

    migrate_project_state(old_layout.control)

    state_dir = project_state_dir(old_layout.control)
    assert not old_layout.legacy.exists()
    assert (state_dir / "worktrees" / LIVE_ID / "README.md").read_text() == "seed\n"
    assert (state_dir / "worktrees" / LIVE_ID / ".imp" / "quests" / LIVE_ID
            ).resolve() == state_dir / "quests" / LIVE_ID
    assert _meta(state_dir, DRAFT_ID)["project_root"] == str(old_layout.control)
    assert str(state_dir / "worktrees" / LIVE_ID) in _git(
        old_layout.control, "worktree", "list").stdout


@pytest.mark.timeout(240)
def test_an_interrupted_cross_device_copy_discards_its_partial_staging(
    old_layout: OldLayout, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec AC 21 + 20b: the source is authoritative until the rename, so the
    half-written staging directory is safe to throw away on the next run."""
    monkeypatch.setattr(state_migrate, "_same_device", lambda a, b: False)
    real_copytree = shutil.copytree

    def flaky_copytree(src: Path, dst: Path, *args: object, **kwargs: object) -> None:
        if src.name == "quests":      # sorted after archive; worktrees comes later
            dst.mkdir(parents=True)
            (dst / "half.txt").write_text("partial copy")
            raise OSError("interrupted")
        real_copytree(src, dst, *args, **kwargs)

    monkeypatch.setattr(state_migrate.shutil, "copytree", flaky_copytree)
    with pytest.raises(OSError, match="interrupted"):
        migrate_project_state(old_layout.control)
    assert (old_layout.legacy / "quests" / LIVE_ID / "quest.json").is_file()

    monkeypatch.setattr(state_migrate.shutil, "copytree", real_copytree)
    migrate_project_state(old_layout.control)

    state_dir = project_state_dir(old_layout.control)
    assert not old_layout.legacy.exists()
    assert not (state_dir / "quests" / "half.txt").exists()
    assert not (state_dir / ".migrating-quests").exists()
    assert _meta(state_dir, LIVE_ID)["project_root"] == str(
        state_dir / "worktrees" / LIVE_ID)

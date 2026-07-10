from __future__ import annotations

from pathlib import Path

from imp.engine.change import Change, link_change_home
from tests.conftest import _seed_control


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    """Return (worktree, change_dir) with the permanent home seeded."""
    change_dir = tmp_path / ".imp" / "changes" / "abc"
    (change_dir / "artifacts" / "spec").mkdir(parents=True)
    (change_dir / "artifacts" / "spec" / "s.md").write_text("home-spec\n")
    worktree = tmp_path / ".imp" / "worktrees" / "abc"
    worktree.mkdir(parents=True)
    return worktree, change_dir


def test_creates_id_scoped_symlink_not_blanket(tmp_path: Path) -> None:
    worktree, change_dir = _layout(tmp_path)
    link_change_home(worktree, change_dir)
    link = worktree / ".imp" / "changes" / "abc"
    assert link.is_symlink()
    assert link.resolve() == change_dir.resolve()
    # Only the id leaf is a symlink; the parents are real dirs (never a blanket
    # <worktree>/.imp link).
    assert not (worktree / ".imp").is_symlink()
    assert not (worktree / ".imp" / "changes").is_symlink()


def test_idempotent_reassert_is_noop(tmp_path: Path) -> None:
    worktree, change_dir = _layout(tmp_path)
    link_change_home(worktree, change_dir)
    link_change_home(worktree, change_dir)          # second call must not raise
    link = worktree / ".imp" / "changes" / "abc"
    assert link.is_symlink()
    assert link.resolve() == change_dir.resolve()


def test_legacy_phantom_folds_into_home_then_links(tmp_path: Path) -> None:
    worktree, change_dir = _layout(tmp_path)
    # A real phantom directory sits where the link belongs, carrying a file the
    # home does NOT have, plus a colliding file the home already owns.
    phantom = worktree / ".imp" / "changes" / "abc"
    (phantom / "artifacts" / "plans").mkdir(parents=True)
    (phantom / "artifacts" / "plans" / "p.md").write_text("phantom-plan\n")
    (phantom / "artifacts" / "spec").mkdir(parents=True)
    (phantom / "artifacts" / "spec" / "s.md").write_text("phantom-spec\n")

    link_change_home(worktree, change_dir)

    link = worktree / ".imp" / "changes" / "abc"
    assert link.is_symlink() and link.resolve() == change_dir.resolve()
    # Phantom-only content was folded into the permanent home (no content loss).
    assert (change_dir / "artifacts" / "plans" / "p.md").read_text() == "phantom-plan\n"
    # On collision the home's copy wins (it is authoritative).
    assert (change_dir / "artifacts" / "spec" / "s.md").read_text() == "home-spec\n"


def test_stale_symlink_is_replaced(tmp_path: Path) -> None:
    worktree, change_dir = _layout(tmp_path)
    stale_target = tmp_path / "other"
    stale_target.mkdir()
    link = worktree / ".imp" / "changes" / "abc"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(stale_target)
    link_change_home(worktree, change_dir)
    assert link.is_symlink()
    assert link.resolve() == change_dir.resolve()


def test_create_links_worktree_home_no_phantom(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", quest_id="cre1", base_branch="main")
    link = ch.project_root / ".imp" / "changes" / "cre1"
    assert link.is_symlink()
    assert link.resolve() == ch.change_dir.resolve()


def test_materialize_links_worktree_home(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="X", quest_id="mat1", base_branch="main")
    # A draft gets no worktree link (project_root is the control root).
    assert not (ch.project_root / ".imp" / "changes" / "mat1").is_symlink()
    ch.materialize()
    link = ch.project_root / ".imp" / "changes" / "mat1"
    assert link.is_symlink()
    assert link.resolve() == ch.change_dir.resolve()


def test_relative_edit_after_golive_lands_in_home(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="X", quest_id="gol1", base_branch="main")
    ch.materialize()
    # A relative path through the worktree symlink writes into the permanent home,
    # not a phantom.
    p = ch.project_root / ".imp" / "changes" / "gol1" / "artifacts" / "note.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("via-symlink\n")
    assert (ch.change_dir / "artifacts" / "note.txt").read_text() == "via-symlink\n"


def test_relink_home_self_heals_legacy_phantom(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", quest_id="heal1", base_branch="main")
    link = ch.project_root / ".imp" / "changes" / "heal1"
    # Simulate an in-flight change whose worktree carries a legacy phantom dir
    # instead of the symlink (e.g. created before this feature shipped).
    link.unlink()
    (link / "artifacts").mkdir(parents=True)
    (link / "artifacts" / "stale.txt").write_text("stale\n")

    ch.relink_home()

    assert link.is_symlink()
    assert link.resolve() == ch.change_dir.resolve()
    assert (ch.change_dir / "artifacts" / "stale.txt").read_text() == "stale\n"


def test_relink_home_noop_for_draft(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="X", quest_id="heal2", base_branch="main")
    ch.relink_home()                              # must not raise, must not link
    assert not (ch.project_root / ".imp" / "changes" / "heal2").is_symlink()


def test_relink_home_noop_when_project_root_is_control_root(tmp_path: Path) -> None:
    change_dir = tmp_path / ".imp" / "changes" / "guard1"
    change_dir.mkdir(parents=True)
    ch = Change(
        id="guard1",
        title="X",
        base_branch="main",
        control_root=tmp_path,
        project_root=tmp_path,
        change_dir=change_dir,
        materialized=True,
    )
    ch.relink_home()
    assert not (tmp_path / ".imp" / "changes" / "guard1").is_symlink()


def test_relative_home_location_is_worktree_symlink_to_home(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="X", quest_id="rel1", base_branch="main")
    ch.materialize()
    rel = f".imp/changes/{ch.id}"
    link = ch.project_root / rel
    assert link.is_symlink()
    assert link.resolve() == ch.change_dir.resolve()
    # A write through the relative location lands in the permanent home.
    p = link / "artifacts" / "note.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("via-rel\n")
    assert (ch.change_dir / "artifacts" / "note.txt").read_text() == "via-rel\n"

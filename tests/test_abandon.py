from pathlib import Path

import pytest

from imp.cli.main import _abandon, _build_parser, _status_rows
from imp.engine import worktree
from imp.engine.abandon import tear_down_change
from imp.engine.quest import Quest
from imp.engine.state import ProjectLock, State
from tests.conftest import _git, _seed_control


def test_remove_worktree_force_tolerates_dirty_tree(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    (ch.project_root / "dirty.txt").write_text("uncommitted")  # plain remove would error
    worktree.remove_worktree(tmp_path, "feat", force=True)
    assert not (tmp_path / ".imp" / "worktrees" / "feat").exists()


def test_remove_worktree_force_tolerates_absent_worktree(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    worktree.remove_worktree(tmp_path, "feat", force=True)
    # Second call: worktree already gone + stale metadata; must not raise.
    worktree.remove_worktree(tmp_path, "feat", force=True)


def test_delete_branch_removes_and_is_idempotent(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    worktree.remove_worktree(tmp_path, "feat", force=True)  # branch must be uncheckedout first
    worktree.delete_branch(tmp_path, "feat")
    assert _git(tmp_path, "branch", "--list", "imp/feat").stdout.strip() == ""
    # Idempotent: deleting an already-deleted branch does not raise.
    worktree.delete_branch(tmp_path, "feat")


def test_abandon_happy_path_tears_down_and_archives(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")

    rc = _abandon(tmp_path, "feat")
    assert rc == 0
    assert not (tmp_path / ".imp" / "worktrees" / "feat").exists()       # worktree gone
    assert _git(tmp_path, "branch", "--list", "imp/feat").stdout.strip() == ""  # branch gone
    assert not (tmp_path / ".imp" / "quests" / "feat").exists()   # root home dropped

    # Durable: change_abandoned event + abandoned status retrievable via archive.
    from imp.engine.archive import read as archive_read
    state_json = archive_read.read_state_json(tmp_path, "feat")
    assert state_json is not None and b'"abandoned"' in state_json
    events = archive_read.read_events(tmp_path, "feat")
    assert events is not None and b"change_abandoned" in events


def test_abandon_surfaces_as_completed_not_inflight(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    _abandon(tmp_path, "feat")

    assert all(r[0] != "feat" for r in _status_rows(tmp_path))           # not in-flight
    from imp.engine.archive import read as archive_read
    assert archive_read.is_archived(tmp_path, "feat")                    # in archive
    assert archive_read.status_of(tmp_path, "feat") == "abandoned"       # labeled abandoned


def test_abandon_allowed_from_completed_status(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "completed"          # completed-but-not-merged: abandon is the only verb
    st.save(ch.state_dir)
    assert _abandon(tmp_path, "feat") == 0
    assert not (tmp_path / ".imp" / "worktrees" / "feat").exists()


def test_abandon_refuses_when_lock_held(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    with ProjectLock(ch.quest_dir / "lock").acquire():     # simulate a live runner
        rc = _abandon(tmp_path, "feat")
    assert rc == 2
    assert (tmp_path / ".imp" / "worktrees" / "feat").exists()  # untouched


def test_abandon_already_gone_is_idempotent_and_reaps_branch(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    # Simulate a crash between (d) worktree removal and (e) branch deletion:
    # worktree gone, branch orphaned.
    worktree.remove_worktree(tmp_path, "feat", force=True)
    assert _git(tmp_path, "branch", "--list", "imp/feat").stdout.strip() != ""  # orphan exists

    rc = _abandon(tmp_path, "feat")
    assert rc == 0                                              # not the rc=2 LoadError path
    assert _git(tmp_path, "branch", "--list", "imp/feat").stdout.strip() == ""  # reaped


def test_abandon_accepts_yes_flag() -> None:
    parser = _build_parser()
    assert parser.parse_args(["abandon", "feat"]).yes is False
    assert parser.parse_args(["abandon", "--yes", "feat"]).yes is True


def test_abandon_materialized_confirms_on_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    monkeypatch.setattr("imp.cli.main._interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _p: "y")
    rc = _abandon(tmp_path, "feat")
    assert rc == 0
    assert "Abandon feat?" in capsys.readouterr().out  # prompt was shown
    assert not (tmp_path / ".imp" / "worktrees" / "feat").exists()  # torn down


def test_abandon_declined_keeps_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    monkeypatch.setattr("imp.cli.main._interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _p: "n")
    rc = _abandon(tmp_path, "feat")
    assert rc == 0
    assert (tmp_path / ".imp" / "worktrees" / "feat").exists()  # untouched
    assert "Left feat in place." in capsys.readouterr().out


def test_abandon_yes_flag_skips_confirm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    monkeypatch.setattr("imp.cli.main._interactive", lambda: True)

    def _boom(_p: str) -> str:
        raise AssertionError("--yes must skip the prompt")

    monkeypatch.setattr("builtins.input", _boom)
    rc = _abandon(tmp_path, "feat", yes=True)
    assert rc == 0
    assert not (tmp_path / ".imp" / "worktrees" / "feat").exists()  # torn down


def test_commit_count_reflects_branch_commits(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    # Fresh worktree sits on main with no new commits.
    assert worktree.commit_count(tmp_path, "feat", "main") == 0
    wt = tmp_path / ".imp" / "worktrees" / "feat"
    (wt / "f.txt").write_text("x")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "work")
    assert worktree.commit_count(tmp_path, "feat", "main") == 1


def test_abandon_resident_routes_through_daemon(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import imp.cli.main as _m
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    delivered: list[str] = []
    monkeypatch.setattr(_m, "session_is_resident", lambda _ctrl, cid: True)
    monkeypatch.setattr(_m, "_deliver_abandon_resident",
                        lambda _ctrl, cid: delivered.append(cid))
    rc = _abandon(tmp_path, "feat")
    assert rc == 0
    assert delivered == ["feat"]


def test_abandon_falls_through_when_resident_daemon_never_acks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.main as main_mod

    _seed_control(tmp_path)
    Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")

    monkeypatch.setattr(main_mod, "session_is_resident", lambda c, cid: True)

    def _hang(_control: Path, _cid: str) -> None:
        raise TimeoutError  # simulate the bounded receive timing out

    torn: list[str] = []
    monkeypatch.setattr(main_mod, "_deliver_abandon_resident", _hang)
    monkeypatch.setattr(main_mod, "tear_down_change", lambda ch: torn.append(ch.id))

    rc = main_mod._abandon(tmp_path, "feat")
    assert rc == 0
    assert torn == ["feat"]  # fell through to direct teardown


def test_tear_down_change_destroys_worktree_branch_and_archives(
    tmp_path: Path,
) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")

    tear_down_change(ch)

    assert not (tmp_path / ".imp" / "worktrees" / "feat").exists()       # worktree gone
    assert _git(tmp_path, "branch", "--list", "imp/feat").stdout.strip() == ""  # branch gone
    assert not (tmp_path / ".imp" / "quests" / "feat").exists()   # root home dropped
    from imp.engine.archive import read as archive_read
    state_json = archive_read.read_state_json(tmp_path, "feat")
    assert state_json is not None and b'"abandoned"' in state_json
    events = archive_read.read_events(tmp_path, "feat")
    assert events is not None and b"change_abandoned" in events


def test_abandon_refuses_when_lock_held_on_idea(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="An idea", quest_id="idea1",
                             base_branch="main", origin="idea",
                             seed_opening="An idea\n\ndo a thing")
    with ProjectLock(ch.quest_dir / "lock").acquire():
        rc = _abandon(tmp_path, "idea1")
    assert rc == 2
    assert (tmp_path / ".imp" / "quests" / "idea1").exists()  # untouched
    assert "stop it before abandoning" in capsys.readouterr().err


def test_abandon_retires_backlog_item(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Quest.create_draft(tmp_path, title="Backlog brief", quest_id="bk1",
                        base_branch="main", origin="followup",
                        seed_opening="Backlog brief\n\ndo a thing")
    rc = _abandon(tmp_path, "bk1")
    assert rc == 0
    assert not (tmp_path / ".imp" / "quests" / "bk1").exists()


def test_abandon_backlog_reports_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_control(tmp_path)
    Quest.create_draft(tmp_path, title="Backlog brief", quest_id="bk1",
                        base_branch="main", origin="followup",
                        seed_opening="Backlog brief")
    rc = _abandon(tmp_path, "bk1")
    assert rc == 0
    assert "Abandoned bk1." in capsys.readouterr().out       # unified message; not "Discarded idea"
    from imp.engine.archive import read as archive_read
    assert archive_read.status_of(tmp_path, "bk1") == "abandoned"  # non-resumable tombstone written
    assert archive_read.is_archived(tmp_path, "bk1")              # findable in the archive
    from imp.engine.archive import record, records_root
    rec = record.read_record(records_root(tmp_path), "bk1")
    assert rec is not None and rec["resumable"] is False          # tombstone is non-resumable


def test_abandon_unknown_id_reaps_branch_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_control(tmp_path)
    rc = _abandon(tmp_path, "ghost")
    assert rc == 0
    assert "already gone" in capsys.readouterr().out


def test_tear_down_change_on_draft_skips_worktree_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.engine import merge_queue, worktree
    _seed_control(tmp_path)
    ch = Quest.create_draft(
        tmp_path, title="An idea", quest_id="idea1", base_branch="main",
        origin="idea", seed_opening="An idea\n\ndo a thing")

    calls: list[str] = []
    monkeypatch.setattr(merge_queue, "pop_and_wake",
                        lambda *a, **k: calls.append("pop_and_wake"))
    monkeypatch.setattr(worktree, "remove_worktree",
                        lambda *a, **k: calls.append("remove_worktree"))
    monkeypatch.setattr(worktree, "delete_branch",
                        lambda *a, **k: calls.append("delete_branch"))

    tear_down_change(ch)

    assert calls == []  # no built-out teardown on a draft
    assert not (tmp_path / ".imp" / "quests" / "idea1").exists()   # live home dropped
    from imp.engine.archive import read as archive_read
    assert archive_read.status_of(tmp_path, "idea1") == "abandoned"  # tombstone marked abandoned
    assert archive_read.is_archived(tmp_path, "idea1")               # findable in the archive
    events = archive_read.read_events(tmp_path, "idea1")
    assert events is not None and b"change_abandoned" in events      # abandon event recorded
    from imp.engine.archive import record, records_root
    rec = record.read_record(records_root(tmp_path), "idea1")
    assert rec is not None and rec["resumable"] is False             # non-resumable tombstone


def test_abandon_draft_confirm_copy_on_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_control(tmp_path)
    Quest.create_draft(tmp_path, title="An idea", quest_id="idea1",
                        base_branch="main", origin="idea",
                        seed_opening="An idea\n\ndo a thing")
    monkeypatch.setattr("imp.cli.main._interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _p: "y")
    rc = _abandon(tmp_path, "idea1")
    out = capsys.readouterr().out
    assert rc == 0
    assert "Abandon idea1? This permanently discards it." in out   # draft-specific copy
    assert "worktree, branch, and state" not in out                # not the built-out copy
    assert not (tmp_path / ".imp" / "quests" / "idea1").exists()   # torn down


def test_resume_refuses_abandoned_draft(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from imp.cli.main import _load_or_rehydrate

    _seed_control(tmp_path)
    Quest.create_draft(tmp_path, title="An idea", quest_id="idea1",
                        base_branch="main", origin="idea",
                        seed_opening="An idea\n\ndo a thing")
    assert _abandon(tmp_path, "idea1") == 0
    capsys.readouterr()  # drop the abandon output

    result = _load_or_rehydrate(tmp_path, "idea1")
    assert result == 0  # int refusal code, not a rehydrated Quest
    assert "was abandoned; nothing to resume." in capsys.readouterr().out


def test_search_finds_abandoned_draft(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from imp.cli.main import _cmd_search

    _seed_control(tmp_path)
    Quest.create_draft(tmp_path, title="Zephyr idea", quest_id="idea1",
                        base_branch="main", origin="idea",
                        seed_opening="Zephyr idea\n\ndo a thing")
    assert _abandon(tmp_path, "idea1") == 0
    capsys.readouterr()  # drop the abandon output

    ns = argparse.Namespace(
        query="Zephyr", active=False, backlog=False, archived=False,
        list=False, no_pager=True)
    rc = _cmd_search(ns, tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "idea1" in out            # abandoned draft is found
    assert "[archive]" in out        # surfaced from the archive zone

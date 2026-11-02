import argparse
import os
import shutil
import uuid
from pathlib import Path

import pytest

from imp.cli.main import _abandon, _abandon_local, _cmd_abandon
from imp.cli.store import _status_rows
from imp.engine import worktree
from imp.engine.abandon import discard_conversation, tear_down_quest
from imp.engine.quest import Quest
from imp.engine.state import ProjectLock, State
from imp.paths import project_state_dir
from tests.conftest import _commit_work, _git, _seed_control

_var_tmp_writable = pytest.mark.skipif(
    not os.access("/var/tmp", os.W_OK),
    reason="requires writable /var/tmp",
)


def test_remove_worktree_force_tolerates_dirty_tree(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    (ch.project_root / "dirty.txt").write_text("uncommitted")  # plain remove would error
    worktree.remove_worktree(tmp_path, "feat", force=True)
    assert not (project_state_dir(tmp_path) / "worktrees" / "feat").exists()


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

    rc = _abandon_local(tmp_path, "feat")
    assert rc == 0
    assert not (project_state_dir(tmp_path) / "worktrees" / "feat").exists()       # worktree gone
    assert _git(tmp_path, "branch", "--list", "imp/feat").stdout.strip() == ""  # branch gone
    assert not (project_state_dir(tmp_path) / "quests" / "feat").exists()   # root home dropped

    # Durable: quest_abandoned event + abandoned status retrievable via archive.
    from imp.engine.archive import read as archive_read
    state_json = archive_read.read_state_json(tmp_path, "feat")
    assert state_json is not None and b'"abandoned"' in state_json
    events = archive_read.read_events(tmp_path, "feat")
    assert events is not None and b"quest_abandoned" in events


def test_imp_abandon_with_a_token_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    monkeypatch.delenv("IMP_SESSION_SOCKET", raising=False)
    monkeypatch.setattr("imp.cli.main._control_root", lambda: tmp_path)

    rc = _cmd_abandon(argparse.Namespace(token="feat", yes=False))

    assert rc == 0                      # the command succeeded, not a user-ended leave
    assert not (project_state_dir(tmp_path) / "worktrees" / "feat").exists()


def test_abandon_surfaces_as_completed_not_inflight(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    _abandon_local(tmp_path, "feat")

    assert all(r[0] != "feat" for r in _status_rows(tmp_path))           # not in-flight
    from imp.engine.archive import read as archive_read
    assert archive_read.is_archived(tmp_path, "feat")                    # in archive
    assert archive_read.status_of(tmp_path, "feat") == "abandoned"       # labeled abandoned


def test_abandon_allowed_from_completed_status(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "merged"              # merged-but-not-archived: abandon is the only verb
    st.save(ch.state_dir)
    assert _abandon_local(tmp_path, "feat") == 0
    assert not (project_state_dir(tmp_path) / "worktrees" / "feat").exists()


def test_abandon_refuses_when_lock_held(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    with ProjectLock(ch.quest_dir / "lock").acquire():     # simulate a live runner
        rc = _abandon_local(tmp_path, "feat")
    assert rc == 2
    assert (project_state_dir(tmp_path) / "worktrees" / "feat").exists()  # untouched


def test_abandon_already_gone_is_idempotent_and_reaps_branch(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    # Simulate a crash between (d) worktree removal and (e) branch deletion:
    # worktree gone, branch orphaned.
    worktree.remove_worktree(tmp_path, "feat", force=True)
    assert _git(tmp_path, "branch", "--list", "imp/feat").stdout.strip() != ""  # orphan exists

    rc = _abandon_local(tmp_path, "feat")
    assert rc == 0                                              # not the rc=2 LoadError path
    assert _git(tmp_path, "branch", "--list", "imp/feat").stdout.strip() == ""  # reaped



def test_commit_count_reflects_branch_commits(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    # Fresh worktree sits on main with no new commits.
    assert worktree.commit_count(tmp_path, "feat", "main") == 0
    wt = project_state_dir(tmp_path) / "worktrees" / "feat"
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
    monkeypatch.setattr("imp.remote.daemon.directory.session_is_resident", lambda _ctrl, cid: True)
    monkeypatch.setattr(_m, "_deliver_abandon_resident",
                        lambda cid: delivered.append(cid))
    rc = _abandon_local(tmp_path, "feat")
    assert rc == 0
    assert delivered == ["feat"]


def test_abandon_falls_through_when_resident_daemon_never_acks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.main as main_mod

    _seed_control(tmp_path)
    Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")

    monkeypatch.setattr("imp.remote.daemon.directory.session_is_resident", lambda c, cid: True)

    def _hang(_cid: str) -> None:
        raise TimeoutError  # simulate the bounded receive timing out

    torn: list[str] = []
    monkeypatch.setattr(main_mod, "_deliver_abandon_resident", _hang)
    monkeypatch.setattr("imp.engine.abandon.tear_down_quest", lambda ch: torn.append(ch.id))

    rc = main_mod._abandon_local(tmp_path, "feat")
    assert rc == 0
    assert torn == ["feat"]  # fell through to direct teardown


def test_tear_down_quest_destroys_worktree_branch_and_archives(
    tmp_path: Path,
) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")

    tear_down_quest(ch)

    assert not (project_state_dir(tmp_path) / "worktrees" / "feat").exists()       # worktree gone
    assert _git(tmp_path, "branch", "--list", "imp/feat").stdout.strip() == ""  # branch gone
    assert not (project_state_dir(tmp_path) / "quests" / "feat").exists()   # root home dropped
    from imp.engine.archive import read as archive_read
    state_json = archive_read.read_state_json(tmp_path, "feat")
    assert state_json is not None and b'"abandoned"' in state_json
    events = archive_read.read_events(tmp_path, "feat")
    assert events is not None and b"quest_abandoned" in events


def test_abandon_refuses_when_lock_held_on_idea(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="An idea", quest_id="idea1",
                             base_branch="main", origin="idea",
                             seed_opening="An idea\n\ndo a thing")
    with ProjectLock(ch.quest_dir / "lock").acquire():
        rc = _abandon_local(tmp_path, "idea1")
    assert rc == 2
    assert (project_state_dir(tmp_path) / "quests" / "idea1").exists()  # untouched
    assert "stop it before abandoning" in capsys.readouterr().err


def test_abandon_retires_backlog_item(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Quest.create_draft(tmp_path, title="Backlog brief", quest_id="bk1",
                        base_branch="main", origin="followup",
                        seed_opening="Backlog brief\n\ndo a thing")
    rc = _abandon_local(tmp_path, "bk1")
    assert rc == 0
    assert not (project_state_dir(tmp_path) / "quests" / "bk1").exists()


def test_abandon_backlog_reports_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_control(tmp_path)
    Quest.create_draft(tmp_path, title="Backlog brief", quest_id="bk1",
                        base_branch="main", origin="followup",
                        seed_opening="Backlog brief")
    rc = _abandon_local(tmp_path, "bk1")
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
    rc = _abandon_local(tmp_path, "ghost")
    assert rc == 0
    assert "already gone" in capsys.readouterr().out


def test_tear_down_quest_on_draft_skips_worktree_teardown(
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

    tear_down_quest(ch)

    assert calls == []  # no built-out teardown on a draft
    assert not (project_state_dir(tmp_path) / "quests" / "idea1").exists()   # live home dropped
    from imp.engine.archive import read as archive_read
    assert archive_read.status_of(tmp_path, "idea1") == "abandoned"  # tombstone marked abandoned
    assert archive_read.is_archived(tmp_path, "idea1")               # findable in the archive
    events = archive_read.read_events(tmp_path, "idea1")
    assert events is not None and b"quest_abandoned" in events      # abandon event recorded
    from imp.engine.archive import record, records_root
    rec = record.read_record(records_root(tmp_path), "idea1")
    assert rec is not None and rec["resumable"] is False             # non-resumable tombstone


def test_abandon_draft_no_prompt_on_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    Quest.create_draft(tmp_path, title="An idea", quest_id="idea1",
                        base_branch="main", origin="idea",
                        seed_opening="An idea\n\ndo a thing")
    monkeypatch.setattr("imp.cli.main._interactive", lambda: True)

    def _boom(_p: str) -> str:
        raise AssertionError("abandon must not prompt")

    monkeypatch.setattr("builtins.input", _boom)
    rc = _abandon_local(tmp_path, "idea1")
    assert rc == 0
    assert not (project_state_dir(tmp_path) / "quests" / "idea1").exists()  # torn down, no prompt


def test_abandon_materialized_no_prompt_on_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    monkeypatch.setattr("imp.cli.main._interactive", lambda: True)

    def _boom(_p: str) -> str:
        raise AssertionError("abandon must not prompt")

    monkeypatch.setattr("builtins.input", _boom)
    rc = _abandon_local(tmp_path, "feat")
    assert rc == 0
    assert not (project_state_dir(tmp_path) / "worktrees" / "feat").exists()  # torn down, no prompt


def test_resume_refuses_abandoned_draft(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from imp.cli.main import _load_or_rehydrate

    _seed_control(tmp_path)
    Quest.create_draft(tmp_path, title="An idea", quest_id="idea1",
                        base_branch="main", origin="idea",
                        seed_opening="An idea\n\ndo a thing")
    assert _abandon_local(tmp_path, "idea1") == 0
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
    assert _abandon_local(tmp_path, "idea1") == 0
    capsys.readouterr()  # drop the abandon output

    ns = argparse.Namespace(
        query="Zephyr", active=False, backlog=False, archived=False,
        merged=False, abandoned=False, shelved=False, merged_after=None,
        list=False, no_pager=True)
    rc = _cmd_search(ns, tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "idea1" in out            # abandoned draft is found
    assert "· archive ·" in out      # surfaced from the archive zone


def test_abandon_returns_rc_and_message_without_printing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    rc, message = _abandon(tmp_path, "feat")
    assert rc == 0
    assert message == "Abandoned feat · removed worktree and branch imp/feat (no commits)."
    assert capsys.readouterr().out == ""        # pure: teardown no longer prints


def test_abandon_local_emits_message_and_returns_rc(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    rc = _abandon_local(tmp_path, "feat")
    assert rc == 0
    assert capsys.readouterr().out == (
        "Abandoned feat · removed worktree and branch imp/feat (no commits).\n")


def test_tear_down_quest_reaps_per_quest_tmp_dir(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    ch.tmp_dir.mkdir(parents=True, exist_ok=True)
    (ch.tmp_dir / "session.sock").write_bytes(b"")   # scratch that must be reaped

    tear_down_quest(ch)

    assert not ch.tmp_dir.exists()                   # per-quest scratch gone


def test_discard_conversation_reaps_per_quest_tmp_dir(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    ch.tmp_dir.mkdir(parents=True, exist_ok=True)
    (ch.tmp_dir / "session.sock").write_bytes(b"")

    discard_conversation(ch)

    assert not ch.tmp_dir.exists()
    assert not (project_state_dir(tmp_path) / "quests" / "feat").exists()   # live home dropped too


@_var_tmp_writable
def test_tear_down_quest_force_reaps_per_quest_var_tmp_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import imp.engine.sandbox as sandbox_mod
    _seed_control(tmp_path)
    qid = f"vt{uuid.uuid4().hex[:8]}"
    ch = Quest.create(tmp_path, title="x", quest_id=qid, base_branch="main")
    # Simulate overlay residue: non-empty mode-000 work/work that plain rmtree leaves.
    residue = ch.var_tmp_dir / "work" / "work"
    residue.mkdir(parents=True, exist_ok=True)
    (residue / "file.txt").write_text("x")  # write file before chmod 000
    (ch.var_tmp_dir / "scratch").write_text("x")
    residue.chmod(0o000)
    # Spy installed just before teardown so setup chmods don't appear in recording.
    real_chmod = os.chmod
    chmodded: list[str] = []
    def _spy_chmod(p, mode, *a, **k):
        chmodded.append(str(p))
        return real_chmod(p, mode, *a, **k)
    monkeypatch.setattr(sandbox_mod.os, "chmod", _spy_chmod)
    try:
        tear_down_quest(ch)
        assert not ch.var_tmp_dir.exists()
        assert str(residue) in chmodded  # proves force_rmtree's chmod walk ran
    finally:
        if residue.exists():
            residue.chmod(0o700)
        shutil.rmtree(ch.var_tmp_dir, ignore_errors=True)


@_var_tmp_writable
def test_discard_conversation_reaps_per_quest_var_tmp_dir(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qid = f"vt{uuid.uuid4().hex[:8]}"
    ch = Quest.create(tmp_path, title="x", quest_id=qid, base_branch="main")
    ch.var_tmp_dir.mkdir(parents=True, exist_ok=True)
    (ch.var_tmp_dir / "scratch").write_text("x")
    try:
        discard_conversation(ch)
        assert not ch.var_tmp_dir.exists()
    finally:
        shutil.rmtree(ch.var_tmp_dir, ignore_errors=True)


def test_tear_down_quest_force_reaps_overlay_residue_in_quest_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import imp.engine.sandbox as sandbox_mod
    _seed_control(tmp_path)
    qid = f"ov{uuid.uuid4().hex[:8]}"
    ch = Quest.create(tmp_path, title="x", quest_id=qid, base_branch="main")
    # Simulate overlayfs residue at steps/<phase>/<step>.overlay/<layer>/work/work
    residue = ch.quest_dir / "steps" / "plan" / "1.overlay" / "layer0" / "work" / "work"
    residue.mkdir(parents=True, exist_ok=True)
    (residue / "file.txt").write_text("x")
    residue.chmod(0o000)
    real_chmod = os.chmod
    chmodded: list[str] = []
    def _spy_chmod(p, mode, *a, **k):
        chmodded.append(str(p))
        return real_chmod(p, mode, *a, **k)
    monkeypatch.setattr(sandbox_mod.os, "chmod", _spy_chmod)
    try:
        tear_down_quest(ch)
        assert not ch.quest_dir.exists()
        assert str(residue) in chmodded  # proves force_rmtree's chmod walk ran
    finally:
        if residue.exists():
            residue.chmod(0o700)
        shutil.rmtree(ch.quest_dir, ignore_errors=True)


def test_abandon_summary_count_captured_before_local_teardown(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    _commit_work(ch, 2)
    rc, message = _abandon(tmp_path, "feat")
    assert rc == 0
    assert message == "Abandoned feat · removed worktree and branch imp/feat (2 commits)."
    assert _git(tmp_path, "branch", "--list", "imp/feat").stdout.strip() == ""  # branch gone


def test_abandon_summary_count_captured_before_resident_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.main as m
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    _commit_work(ch, 1)
    monkeypatch.setattr("imp.remote.daemon.directory.session_is_resident",
                        lambda _ctrl, _cid: True)

    def _daemon_tears_down(cid: str) -> None:
        tear_down_quest(Quest.load(tmp_path, cid))

    monkeypatch.setattr(m, "_deliver_abandon_resident", _daemon_tears_down)
    rc, message = _abandon(tmp_path, "feat")
    assert rc == 0
    assert message == "Abandoned feat · removed worktree and branch imp/feat (1 commit)."
    assert _git(tmp_path, "branch", "--list", "imp/feat").stdout.strip() == ""  # daemon tore down

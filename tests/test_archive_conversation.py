from __future__ import annotations

from pathlib import Path

from imp.engine.abandon import archive_conversation
from imp.engine.archive import read as archive_read
from imp.engine.archive import record, records_root
from imp.engine.change import Change
from tests.conftest import _git, _seed_control


def test_archive_conversation_materialized_tears_down_resumable(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", change_id="conv", base_branch="main")

    archive_conversation(ch)

    assert not (tmp_path / ".imp" / "worktrees" / "conv").exists()          # worktree gone
    assert _git(tmp_path, "branch", "--list", "imp/conv").stdout.strip() == ""  # branch gone
    assert not (tmp_path / ".imp" / "changes" / "conv").exists()            # live home dropped

    # Archived, resumable, NOT abandoned.
    rec = archive_read.read_change_json(tmp_path, "conv")
    assert rec is not None
    assert archive_read.status_of(tmp_path, "conv") == "paused"             # not "abandoned"
    events = archive_read.read_events(tmp_path, "conv")
    assert events is not None and b"conversation_archived" in events
    assert b"change_abandoned" not in events
    full = record.read_record(records_root(tmp_path), "conv")
    assert full is not None and full["resumable"] is True


def test_archive_conversation_unmaterialized_draft_has_no_worktree(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="x", change_id="draft", base_branch="main")
    assert not ch.materialized

    archive_conversation(ch)  # must not raise on absent worktree/branch

    assert not (tmp_path / ".imp" / "changes" / "draft").exists()
    full = record.read_record(records_root(tmp_path), "draft")
    assert full is not None
    assert full["resumable"] is True
    assert archive_read.status_of(tmp_path, "draft") == "paused"
    assert full["change_meta"]["materialized"] is False        # faithful draft state preserved

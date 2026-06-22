from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.abandon import archive_conversation
from imp.engine.archive import read as archive_read
from imp.engine.change import Change
from imp.engine.state import State
from tests.conftest import _seed_control


def test_rehydrate_restores_loadable_draft_from_archive(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="Conv", change_id="draft", base_branch="main")
    orig_head = ch.orig_base_head
    archive_conversation(ch)  # drops the live home into the archive
    assert not (tmp_path / ".imp" / "changes" / "draft").exists()

    change_dir = archive_read.rehydrate_change(tmp_path, "draft")
    assert change_dir == tmp_path / ".imp" / "changes" / "draft"

    # The rehydrated home is a faithful, loadable Change.
    loaded = Change.load(tmp_path, "draft")
    assert loaded.materialized is False
    assert loaded.orig_base_head == orig_head
    state = State.load(loaded.state_dir)
    assert state.status == "paused"  # the left-conversation resting status
    events = (change_dir / "events.jsonl").read_bytes()
    assert b"change_created" in events  # the original conversation events survive


def test_rehydrate_refuses_to_clobber_existing_home(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create_draft(tmp_path, title="Conv", change_id="draft", base_branch="main")
    with pytest.raises(FileExistsError):
        archive_read.rehydrate_change(tmp_path, "draft")


def test_rehydrate_unknown_change_raises(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    with pytest.raises(FileNotFoundError):
        archive_read.rehydrate_change(tmp_path, "nope")

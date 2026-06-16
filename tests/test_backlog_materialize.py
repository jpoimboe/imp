from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.engine import backlog
from tests.conftest import _seed_control


def test_materialize_creates_worktree_and_removes_record(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    backlog.write_record(
        tmp_path, change_id="2026-06-16-ddd111", title="Add OAuth",
        brief="wire OAuth", depends_on=["2026-06-16-dep001"],
        created_by="step:finalize")

    change = backlog.materialize_record(
        tmp_path, "2026-06-16-ddd111", base_branch="main")

    assert change.id == "2026-06-16-ddd111"
    assert change.title == "Add OAuth"
    meta = json.loads((change.change_dir / "change.json").read_text())
    assert meta["depends_on"] == ["2026-06-16-dep001"]
    assert meta["base_branch"] == "main"
    # The record graduated — its file is gone.
    assert backlog.read_record(tmp_path, "2026-06-16-ddd111") is None


def test_materialize_missing_record_raises(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    with pytest.raises(FileNotFoundError, match="no backlog record"):
        backlog.materialize_record(tmp_path, "2026-06-16-nope00", base_branch="main")

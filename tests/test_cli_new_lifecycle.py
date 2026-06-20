"""`imp new` mints an immutable dateless id; two consecutive creates yield two
distinct ids with no retry loop (spec §5, AC 7)."""

import re
from pathlib import Path

from imp.engine.change import Change
from imp.engine.change_id import new_change_id
from tests.conftest import _seed_control

_ID_RE = re.compile(r"[a-z0-9]{6}")


def test_new_mints_unique_dateless_id(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    change_id = new_change_id()
    change = Change.create(tmp_path, title="Add OAuth", change_id=change_id,
                           base_branch="main")
    assert change.id == change_id
    assert _ID_RE.fullmatch(change.id)
    worktrees = sorted((tmp_path / ".imp" / "worktrees").iterdir())
    assert len(worktrees) == 1
    assert worktrees[0].name == change_id


def test_two_new_calls_make_distinct_ids(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    id1 = new_change_id()
    Change.create(tmp_path, title="One", change_id=id1, base_branch="main")
    id2 = new_change_id()
    Change.create(tmp_path, title="Two", change_id=id2, base_branch="main")
    ids = [p.name for p in (tmp_path / ".imp" / "worktrees").iterdir() if p.is_dir()]
    assert len(ids) == 2 and len(set(ids)) == 2
    assert all(_ID_RE.fullmatch(i) for i in ids)

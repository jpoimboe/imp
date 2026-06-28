from pathlib import Path

import anyio

from imp.engine.change import Change
from imp.engine.executor import _route_merge_and_exit_worktree
from imp.engine.state import JsonlEventLog, State
from imp.engine.types import Continue, Pause, Rule, SourceLoc, Step
from tests.conftest import _git, _seed_control


def _merge_exit_step():
    loc = SourceLoc(file=Path("t"), line=1)
    return Step(name="merge-to-base", phase="merge",
                kind="merge_and_exit_worktree",
                rules=(Rule(condition=None, action=Continue(), mutations=(), source=loc),),
                on_max_retries=Pause(), max_loops=1000, source=loc)


def test_merge_and_exit_removes_worktree_and_flips_root(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    (ch.project_root / "feature.txt").write_text("hi")
    _git(ch.project_root, "add", "-A")
    _git(ch.project_root, "commit", "-qm", "feat")
    state, log = State.load(ch.state_dir), JsonlEventLog(ch.events_path)

    action, _fb, _muts = anyio.run(
        _route_merge_and_exit_worktree, _merge_exit_step(), ch, state, log)

    assert not isinstance(action, Pause)
    assert state.merge_commit is not None
    assert (tmp_path / "feature.txt").read_text() == "hi"             # merged into base
    assert not (tmp_path / ".imp" / "worktrees" / "feat").exists()    # worktree removed
    assert ch.project_root == ch.control_root                         # project_root flipped


def test_merge_and_exit_idempotent_when_already_merged(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    (ch.project_root / "feature.txt").write_text("hi")
    _git(ch.project_root, "add", "-A")
    _git(ch.project_root, "commit", "-qm", "feat")
    state, log = State.load(ch.state_dir), JsonlEventLog(ch.events_path)
    state.merge_commit = "deadbeef"                  # pretend a prior run merged
    base_before = _git(tmp_path, "rev-parse", "main").stdout.strip()

    action, _fb, _muts = anyio.run(
        _route_merge_and_exit_worktree, _merge_exit_step(), ch, state, log)

    assert not isinstance(action, Pause)
    assert _git(tmp_path, "rev-parse", "main").stdout.strip() == base_before  # no 2nd merge
    assert not (tmp_path / ".imp" / "worktrees" / "feat").exists()            # still exits
    assert ch.project_root == ch.control_root

from pathlib import Path

import anyio

from imp.engine.executor import _route_merge_and_exit_worktree
from imp.engine.quest import Quest
from imp.engine.state import JsonlEventLog, State
from imp.engine.types import Continue, Goto, Pause, Rule, SourceLoc, Step
from imp.engine.worktree import MergeResult
from imp.paths import project_state_dir
from tests.conftest import _git, _seed_control


def _merge_exit_step():
    loc = SourceLoc(file=Path("t"), line=1)
    return Step(name="merge-to-base", phase="merge",
                kind="merge_and_exit_worktree",
                rules=(Rule(condition=None, action=Continue(), mutations=(), source=loc),),
                on_max_retries=Pause(), max_loops=1000, source=loc)


def test_merge_and_exit_removes_worktree_and_flips_root(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    (ch.project_root / "feature.txt").write_text("hi")
    _git(ch.project_root, "add", "-A")
    _git(ch.project_root, "commit", "-qm", "feat")
    state, log = State.load(ch.state_dir), JsonlEventLog(ch.events_path)

    action, _fb, _muts = anyio.run(
        _route_merge_and_exit_worktree, _merge_exit_step(), ch, state, log)

    assert not isinstance(action, Pause)
    assert state.merge_commit is not None
    assert (tmp_path / "feature.txt").read_text() == "hi"             # merged into base
    assert not (project_state_dir(tmp_path) / "worktrees" / "feat").exists()    # worktree removed
    assert ch.project_root == ch.control_root                         # project_root flipped


def test_merge_and_exit_idempotent_when_already_merged(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
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
    assert not (project_state_dir(tmp_path) / "worktrees" / "feat").exists()  # still exits
    assert ch.project_root == ch.control_root


def _merge_recovery_step(max_loops: int) -> Step:
    loc = SourceLoc(file=Path("t"), line=1)
    # Catch-all Goto: any non-MERGED status routes to a recovery target, so the
    # exhaustion branch in _route_merge_and_exit_worktree fires.
    return Step(name="merge-to-base", phase="merge",
                kind="merge_and_exit_worktree",
                rules=(Rule(condition=None, action=Goto(target="merge-to-base"),
                            mutations=(), source=loc),),
                on_max_retries=Pause(), max_loops=max_loops, source=loc)


def test_merge_recovery_pause_resets_loop_counter(
    tmp_path: Path, monkeypatch,
) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    state, log = State.load(ch.state_dir), JsonlEventLog(ch.events_path)

    from imp.engine import worktree
    monkeypatch.setattr(
        worktree, "merge_to_base",
        lambda *a, **k: MergeResult("CONFLICT", detail="boom"))

    step = _merge_recovery_step(max_loops=3)
    state.loop_counts[step.name] = 3                 # budget already exhausted

    action, _fb, _muts = anyio.run(
        _route_merge_and_exit_worktree, step, ch, state, log)

    assert isinstance(action, Pause)                 # paused, budget spent
    # Reset-on-pause: the counter is cleared so a resumed merge starts fresh.
    assert state.loop_counts.get(step.name, 0) == 0

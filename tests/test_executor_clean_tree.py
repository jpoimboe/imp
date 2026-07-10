from __future__ import annotations

from pathlib import Path

from imp.engine.change import Change
from imp.engine.executor import _enforce_clean_tree
from imp.engine.state import InMemoryEventLog, State
from imp.engine.types import Goto, Pause, SourceLoc, Step
from tests.conftest import _seed_control


def _agent_step(*, name: str = "task-lead", phase: str = "implement",
                clean_tree: bool = True) -> Step:
    return Step(
        name=name, phase=phase, kind="agent", rules=(),
        on_max_retries=Pause(), max_loops=10,
        source=SourceLoc(file=Path("wf.yaml"), line=1),
        clean_tree=clean_tree,
    )


def _materialized_change(tmp_path: Path) -> Change:
    _seed_control(tmp_path)
    return Change.create(tmp_path, title="x", quest_id="feat", base_branch="main")


def _key(step: Step) -> str:
    return f"step:{step.phase}/{step.name}"


def test_clean_worktree_proceeds_and_leaves_no_bounce(tmp_path: Path) -> None:
    ch = _materialized_change(tmp_path)                 # fresh fork => clean
    state = State.load(ch.state_dir)
    step = _agent_step()
    assert _enforce_clean_tree(step, ch, state, InMemoryEventLog()) is None
    assert state.clean_tree_bounce_count(_key(step)) == 0


def test_dirty_worktree_bounces_with_diagnostic_and_bumps_counter(tmp_path: Path) -> None:
    ch = _materialized_change(tmp_path)
    (ch.project_root / "scratch.txt").write_text("uncommitted\n")
    state = State.load(ch.state_dir)
    step = _agent_step()
    log = InMemoryEventLog()
    result = _enforce_clean_tree(step, ch, state, log)
    assert result is not None
    action, feedback, muts = result
    assert isinstance(action, Goto)
    assert action.target == "task-lead"
    assert action.resume is True
    assert feedback is not None and "scratch.txt" in feedback
    assert muts == {}
    assert state.clean_tree_bounce_count(_key(step)) == 1
    # The bounce is journalled (visible in `imp log`), not silent.
    assert any(e["type"] == "step_completed" for e in log.read())


def test_dirty_worktree_at_cap_pauses(tmp_path: Path) -> None:
    ch = _materialized_change(tmp_path)
    (ch.project_root / "scratch.txt").write_text("x\n")
    state = State.load(ch.state_dir)
    step = _agent_step()
    state.clean_tree_bounces[_key(step)] = state.max_clean_tree_bounces  # already at cap
    log = InMemoryEventLog()
    action, _feedback, _muts = _enforce_clean_tree(step, ch, state, log)
    assert isinstance(action, Pause)
    assert any(e["type"] == "step_completed" for e in log.read())


def test_opt_out_step_proceeds_even_when_dirty(tmp_path: Path) -> None:
    ch = _materialized_change(tmp_path)
    (ch.project_root / "scratch.txt").write_text("x\n")
    state = State.load(ch.state_dir)
    step = _agent_step(name="autosquash-resolve-merge", phase="merge", clean_tree=False)
    assert _enforce_clean_tree(step, ch, state, InMemoryEventLog()) is None


def test_no_worktree_before_go_live_is_noop(tmp_path: Path) -> None:
    # A draft: project_root == control_root (no dedicated worktree yet). Even a
    # dirty control root must not trigger the check.
    _seed_control(tmp_path)
    quest_dir = tmp_path / ".imp" / "changes" / "draft"
    (quest_dir / "state").mkdir(parents=True)
    ch = Change(
        id="draft", title="t", base_branch="main",
        control_root=tmp_path, project_root=tmp_path, quest_dir=quest_dir,
        materialized=False,
    )
    State(id="draft", step="task-lead", step_attempt=0, status="running").save(ch.state_dir)
    (tmp_path / "dirty.txt").write_text("x\n")
    state = State.load(ch.state_dir)
    assert _enforce_clean_tree(_agent_step(), ch, state, InMemoryEventLog()) is None


def test_non_git_flipped_project_root_is_noop(tmp_path: Path) -> None:
    # project_root differs from control_root but is NOT a real git worktree (a
    # fake/flipped test root, as in test_sandbox_policy_rebuilt_after_project_root_flip).
    # The is_worktree guard must no-op instead of raising `git status` on a non-repo.
    # tmp_path is the pytest temp dir (not inside any git repo), so its subdirs
    # are non-worktrees.
    control = tmp_path / "control"
    control.mkdir()
    fake_root = tmp_path / "flipped"      # bare dir, never git-inited
    fake_root.mkdir()
    quest_dir = control / ".imp" / "changes" / "flip"
    (quest_dir / "state").mkdir(parents=True)
    ch = Change(
        id="flip", title="t", base_branch="main",
        control_root=control, project_root=fake_root, quest_dir=quest_dir,
        materialized=True,
    )
    State(id="flip", step="task-lead", step_attempt=0, status="running").save(ch.state_dir)
    state = State.load(ch.state_dir)
    assert _enforce_clean_tree(_agent_step(), ch, state, InMemoryEventLog()) is None


def test_clean_pass_after_a_bounce_resets_counter(tmp_path: Path) -> None:
    ch = _materialized_change(tmp_path)
    scratch = ch.project_root / "scratch.txt"
    scratch.write_text("x\n")
    state = State.load(ch.state_dir)
    step = _agent_step()
    _enforce_clean_tree(step, ch, state, InMemoryEventLog())        # bounce -> count 1
    assert state.clean_tree_bounce_count(_key(step)) == 1
    scratch.unlink()                                                # now clean
    assert _enforce_clean_tree(step, ch, state, InMemoryEventLog()) is None
    assert state.clean_tree_bounce_count(_key(step)) == 0           # reset

# tests/test_concurrent_changes.py — integration tests for concurrent changes,
# serialized merges, and gitignore isolation.
from __future__ import annotations

import os
from pathlib import Path

import anyio
import pytest

from imp.engine.change import Change
from imp.engine.executor import RunResult, _complete, run_change
from imp.engine.state import JsonlEventLog, State
from tests.conftest import _git, _seed_control


def test_two_changes_isolated(tmp_path: Path) -> None:
    """Two concurrent worktrees see only their own files."""
    _seed_control(tmp_path)
    a = Change.create(tmp_path, title="A", quest_id="alpha", base_branch="main")
    b = Change.create(tmp_path, title="B", quest_id="beta", base_branch="main")
    (a.project_root / "a.txt").write_text("a")
    (b.project_root / "b.txt").write_text("b")
    assert not (a.project_root / "b.txt").exists()
    assert not (b.project_root / "a.txt").exists()


def test_serialized_merges_second_against_advanced_base(tmp_path: Path) -> None:
    """Two changes merge in sequence; the second picks up what the first committed.
    B3: the merge runs through the engine-native merge-to-base step
    (_route_merge_and_exit_worktree), then _complete finalizes."""
    import anyio

    from imp.engine.executor import _route_merge_and_exit_worktree
    from imp.engine.loader import load_workflow

    _seed_control(tmp_path)
    wf = load_workflow(
        Path(__file__).resolve().parents[1]
        / "src" / "imp" / "assets" / "workflows" / "imp")
    merge_step = next(s for s in wf.flat_steps() if s.name == "merge-to-base")

    a = Change.create(tmp_path, title="A", quest_id="alpha", base_branch="main")
    b = Change.create(tmp_path, title="B", quest_id="beta", base_branch="main")
    for ch, fn in ((a, "a.txt"), (b, "b.txt")):
        (ch.project_root / fn).write_text(fn)
        _git(ch.project_root, "add", "-A")
        _git(ch.project_root, "commit", "-qm", fn)

    for ch in (a, b):
        st = State.load(ch.state_dir)
        log = JsonlEventLog(ch.events_path)
        anyio.run(_route_merge_and_exit_worktree, merge_step, ch, st, log)
        assert st.merge_commit is not None              # merged under the lock
        result = _complete(ch, st, log)
        assert result is RunResult.COMPLETED

    assert (tmp_path / "a.txt").exists() and (tmp_path / "b.txt").exists()


def test_add_a_inside_worktree_ignores_imp_state(tmp_path: Path) -> None:
    """git add -A inside a worktree must NOT stage .imp/ files."""
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", quest_id="iso", base_branch="main")
    (ch.project_root / "src.txt").write_text("code")
    _git(ch.project_root, "add", "-A")
    staged = _git(ch.project_root, "diff", "--cached", "--name-only").stdout
    assert ".imp/" not in staged


def test_per_change_lock_is_exclusive_but_cross_change_is_not(tmp_path: Path) -> None:
    """Same-change lock is exclusive; different-change locks are independent."""
    from imp.engine.state import LockHeld, ProjectLock

    _seed_control(tmp_path)
    a = Change.create(tmp_path, title="A", quest_id="alpha", base_branch="main")
    b = Change.create(tmp_path, title="B", quest_id="beta", base_branch="main")
    with ProjectLock(a.change_dir / "lock").acquire():
        # Same lock file: must raise LockHeld (timeout=0 → immediate)
        with pytest.raises(LockHeld), ProjectLock(a.change_dir / "lock").acquire(timeout=0.2):
            pass
        # Different change's lock file: must succeed
        with ProjectLock(b.change_dir / "lock").acquire(timeout=0.2):
            pass


def test_full_run_to_merge_spine(tmp_path: Path, provider_registry: object) -> None:
    """Full run_change path: workflow completes, the engine-native merge-to-base
    step merges into base, _complete journals artifacts, removes worktree, keeps
    branch (B3 cutover: the merge moved out of _complete into the merge step)."""
    from tests.test_executor_rename import _FakeProvider, _seed_two_step_control

    provider_registry("anthropic", lambda: True, lambda model: _FakeProvider(model))  # type: ignore[call-arg]
    _seed_two_step_control(tmp_path)
    # Append a merge phase so the run exercises the real engine-native merge.
    src_wf = tmp_path / "imp" / "workflows" / "imp" / "workflow.yaml"
    src_wf.write_text(
        src_wf.read_text()
        + "  - name: merge\n    steps:\n"
        "      - name: merge-to-base\n"
        "        kind: merge_and_exit_worktree\n"
        "        rules:\n"
        "          - {if: {merge: MERGED}, continue: true}\n"
        "          - {else: true, pause: true}\n"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "add merge phase")
    ch = Change.create(tmp_path, title="x", quest_id="spine", base_branch="main")
    (ch.project_root / "out.txt").write_text("done")
    _git(ch.project_root, "add", "-A")
    _git(ch.project_root, "commit", "-qm", "work")

    cwd_before = os.getcwd()
    result = anyio.run(run_change, ch)
    assert result is RunResult.COMPLETED
    assert (tmp_path / "out.txt").read_text() == "done"          # merged into live base
    assert not (tmp_path / ".imp" / "worktrees" / "spine").exists()  # worktree removed
    branch_list = _git(tmp_path, "branch", "--list", "imp/spine").stdout
    assert "imp/spine" in branch_list                            # branch kept
    from imp.engine.archive import read as archive_read
    assert archive_read.is_archived(tmp_path, "spine")           # archived (index)
    assert os.getcwd() == cwd_before                             # cwd restored


def test_workflow_edits_isolated_between_changes(tmp_path: Path) -> None:
    """Editing Change A's workflow agent leaves Change B's copy untouched —
    each worktree resolves imp/workflows/imp to its own checkout."""
    _seed_control(tmp_path)
    a = Change.create(tmp_path, title="A", quest_id="alpha", base_branch="main")
    b = Change.create(tmp_path, title="B", quest_id="beta", base_branch="main")

    a_agent = a.workflow_dir / "agents" / "stub.md"
    b_agent = b.workflow_dir / "agents" / "stub.md"
    before = b_agent.read_text()

    a_agent.write_text(a_agent.read_text() + "\nEDITED IN A\n")

    assert "EDITED IN A" in a_agent.read_text()
    assert b_agent.read_text() == before


def test_committed_workflow_edit_is_tracked_on_branch(tmp_path: Path) -> None:
    """Editing + committing a Change's workflow agent produces a real tracked
    diff on imp/<slug>. (In imp's own repo imp/workflows/imp is a symlink, so
    the tracked path is src/imp/assets/workflows/imp/...; the test fixture
    uses a real dir, so the path here is imp/workflows/imp/...)."""
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", quest_id="ver", base_branch="main")

    agent_md = ch.workflow_dir / "agents" / "stub.md"
    agent_md.write_text(agent_md.read_text() + "\nVERSIONED EDIT\n")
    _git(ch.project_root, "add", "-A")
    _git(ch.project_root, "commit", "-qm", "improve stub agent")

    # The edit is a committed diff on the Change's branch...
    show = _git(tmp_path, "show", "imp/ver:imp/workflows/imp/agents/stub.md")
    assert "VERSIONED EDIT" in show.stdout
    # ...and is NOT on base.
    base = _git(tmp_path, "show", "main:imp/workflows/imp/agents/stub.md")
    assert "VERSIONED EDIT" not in base.stdout

# tests/test_executor_merge.py — import _git/_seed_control from tests.conftest
from pathlib import Path

from imp.engine.change import Change
from imp.engine.executor import _complete
from imp.engine.state import JsonlEventLog, State
from tests.conftest import _git, _seed_control


def _ready(ch) -> tuple:
    state = State.load(ch.state_dir)
    state.vars["ready_to_merge"] = True
    return state, JsonlEventLog(ch.events_path)


def test_complete_merges_journals_and_removes(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", slug="feat", base_branch="main")
    (ch.project_root / "feature.txt").write_text("hi")
    _git(ch.project_root, "add", "-A")
    _git(ch.project_root, "commit", "-qm", "feat")
    state, log = _ready(ch)

    result = _complete(ch, state, log)
    assert result.value == "completed"
    assert (tmp_path / "feature.txt").read_text() == "hi"          # merged into base
    assert not (tmp_path / ".imp" / "worktrees" / "feat").exists() # worktree removed
    assert "imp/feat" in _git(tmp_path, "branch", "--list", "imp/feat").stdout  # branch kept
    assert _git(tmp_path, "show", "refs/imp/journal:feat/change.json").returncode == 0

    # The journal captures the full terminal snapshot: state.json with the
    # completed status, and events.jsonl with the change_merged event.
    journaled_state = _git(tmp_path, "show", "refs/imp/journal:feat/state.json").stdout
    assert '"completed"' in journaled_state
    journaled_events = _git(tmp_path, "show", "refs/imp/journal:feat/events.jsonl").stdout
    assert "change_merged" in journaled_events


def test_complete_merge_blocked_on_conflict(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", slug="feat", base_branch="main")
    (ch.project_root / "README.md").write_text("branch")
    _git(ch.project_root, "add", "-A")
    _git(ch.project_root, "commit", "-qm", "b")
    (tmp_path / "README.md").write_text("base")
    _git(tmp_path, "commit", "-qam", "base")
    state, log = _ready(ch)

    result = _complete(ch, state, log)
    assert result.value == "paused"
    assert State.load(ch.state_dir).status == "merge_blocked"
    assert (tmp_path / ".imp" / "worktrees" / "feat").is_dir()  # left intact


def test_complete_refuses_dirty_worktree(tmp_path: Path) -> None:
    # H1: uncommitted work must NOT be silently dropped at merge.
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", slug="feat", base_branch="main")
    (ch.project_root / "uncommitted.txt").write_text("not committed")  # left dirty
    state, log = _ready(ch)

    result = _complete(ch, state, log)
    assert result.value == "paused"
    assert State.load(ch.state_dir).status == "merge_blocked"
    assert (ch.project_root / "uncommitted.txt").exists()        # nothing lost
    assert (tmp_path / ".imp" / "worktrees" / "feat").is_dir()   # worktree intact


def test_merge_blocked_resumes_and_completes(tmp_path: Path) -> None:
    # V8: after the user commits the leftover work, a resume re-enters _complete
    # (ready_to_merge persisted in state.vars) and the merge succeeds.
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", slug="feat", base_branch="main")
    (ch.project_root / "f.txt").write_text("v")  # dirty -> first attempt blocks
    state, log = _ready(ch)
    assert _complete(ch, state, log).value == "paused"

    _git(ch.project_root, "add", "-A")
    _git(ch.project_root, "commit", "-qm", "f")
    state = State.load(ch.state_dir)
    state.status = "running"
    assert state.vars["ready_to_merge"] is True  # persisted across the block
    result = _complete(ch, state, JsonlEventLog(ch.events_path))
    assert result.value == "completed"
    assert (tmp_path / "f.txt").read_text() == "v"
    assert not (tmp_path / ".imp" / "worktrees" / "feat").exists()

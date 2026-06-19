# tests/test_executor_merge.py — import _git/_seed_control from tests.conftest
#
# B3 cutover: the authoritative merge moved out of _complete into the routable
# `merge-to-base` step (_route_merge). These tests drive _route_merge to perform
# the merge (stashing State.merge_commit) and then _complete to finalize
# (journal + remove worktree + mark completed), mirroring the run_change flow.
from pathlib import Path

import anyio

from imp.engine.change import Change
from imp.engine.executor import _complete, _route_merge
from imp.engine.loader import load_workflow
from imp.engine.state import JsonlEventLog, State
from imp.engine.types import Pause
from tests.conftest import _git, _seed_control


def _run_merge(step, ch, state, log):
    """_route_merge is a coroutine (it offloads the blocking git merge off the
    event loop); drive it to completion from these sync tests."""
    return anyio.run(_route_merge, step, ch, state, log)


def _merge_step():
    """The shipped `merge-to-base` step (kind: merge) with its real rules."""
    wf = load_workflow(
        Path(__file__).resolve().parents[1]
        / "src" / "imp" / "assets" / "workflows" / "imp")
    for s in wf.flat_steps():
        if s.name == "merge-to-base":
            return s
    raise AssertionError("merge-to-base step not found in default workflow")


def _ctx(ch) -> tuple:
    return State.load(ch.state_dir), JsonlEventLog(ch.events_path)


def test_merge_step_merges_then_complete_journals_and_removes(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    (ch.project_root / "feature.txt").write_text("hi")
    _git(ch.project_root, "add", "-A")
    _git(ch.project_root, "commit", "-qm", "feat")
    state, log = _ctx(ch)

    action, _fb, _muts = _run_merge(_merge_step(), ch, state, log)
    # MERGED -> continue; commit stashed for _complete to finalize.
    assert not isinstance(action, Pause)
    assert state.merge_commit is not None
    assert (tmp_path / "feature.txt").read_text() == "hi"          # merged into base

    (ch.change_dir / "steps").mkdir(parents=True, exist_ok=True)
    (ch.change_dir / "steps" / "001.json").write_text("{}")

    result = _complete(ch, state, log)
    assert result.value == "completed"
    assert not (tmp_path / ".imp" / "worktrees" / "feat").exists() # worktree removed
    assert not (ch.change_dir / "steps").exists()                  # transcript dir dropped
    assert "imp/feat" in _git(tmp_path, "branch", "--list", "imp/feat").stdout  # branch kept
    assert _git(tmp_path, "show", "refs/imp/journal:feat/change.json").returncode == 0

    # The journal captures the full terminal snapshot: state.json with the
    # completed status, and events.jsonl with the change_merged event.
    journaled_state = _git(tmp_path, "show", "refs/imp/journal:feat/state.json").stdout
    assert '"completed"' in journaled_state
    journaled_events = _git(tmp_path, "show", "refs/imp/journal:feat/events.jsonl").stdout
    assert "change_merged" in journaled_events


def test_merge_step_merge_blocked_on_conflict(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    (ch.project_root / "README.md").write_text("branch")
    _git(ch.project_root, "add", "-A")
    _git(ch.project_root, "commit", "-qm", "b")
    (tmp_path / "README.md").write_text("base")
    _git(tmp_path, "commit", "-qam", "base")
    state, log = _ctx(ch)

    action, _fb, _muts = _run_merge(_merge_step(), ch, state, log)
    # _route_merge intercepts CONFLICT into a reasoned merge_blocked Pause.
    assert isinstance(action, Pause)
    assert action.reason == "merge_blocked"
    assert state.merge_commit is None
    events = list(JsonlEventLog(ch.events_path).read())
    assert any(e.get("type") == "merge_blocked" for e in events)
    assert (tmp_path / ".imp" / "worktrees" / "feat").is_dir()  # left intact


def test_merge_step_refuses_dirty_worktree(tmp_path: Path) -> None:
    # H1/R5: uncommitted work must NOT be silently dropped at merge. The dirty
    # net now lives in merge_to_base under the lock and surfaces as DIRTY ->
    # merge_blocked(worktree_dirty).
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    (ch.project_root / "uncommitted.txt").write_text("not committed")  # left dirty
    state, log = _ctx(ch)

    action, _fb, _muts = _run_merge(_merge_step(), ch, state, log)
    assert isinstance(action, Pause)
    assert action.reason == "merge_blocked"
    assert action.kind == "worktree_dirty"
    assert state.merge_commit is None
    assert (ch.project_root / "uncommitted.txt").exists()        # nothing lost
    assert (tmp_path / ".imp" / "worktrees" / "feat").is_dir()   # worktree intact


def test_merge_blocked_resumes_and_completes(tmp_path: Path) -> None:
    # V8: after the user commits the leftover work, a re-run of the merge step
    # succeeds and _complete finalizes.
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    (ch.project_root / "f.txt").write_text("v")  # dirty -> first attempt blocks
    state, log = _ctx(ch)
    action, _fb, _muts = _run_merge(_merge_step(), ch, state, log)
    assert isinstance(action, Pause)
    assert state.merge_commit is None

    _git(ch.project_root, "add", "-A")
    _git(ch.project_root, "commit", "-qm", "f")
    state = State.load(ch.state_dir)
    action, _fb, _muts = _run_merge(_merge_step(), ch, state, JsonlEventLog(ch.events_path))
    assert not isinstance(action, Pause)
    assert state.merge_commit is not None
    result = _complete(ch, state, JsonlEventLog(ch.events_path))
    assert result.value == "completed"
    assert (tmp_path / "f.txt").read_text() == "v"
    assert not (tmp_path / ".imp" / "worktrees" / "feat").exists()


def test_merge_step_idempotent_when_commit_already_recorded(tmp_path: Path) -> None:
    # Crash-safety: a prior run merged (state.merge_commit set) but crashed before
    # _complete finalized. Re-entering the merge step must NOT re-merge; it routes
    # MERGED via the short-circuit and falls through to _complete.
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    (ch.project_root / "feature.txt").write_text("hi")
    _git(ch.project_root, "add", "-A")
    _git(ch.project_root, "commit", "-qm", "feat")
    state, log = _ctx(ch)
    state.merge_commit = "deadbeef"  # pretend a prior run already merged
    base_before = _git(tmp_path, "rev-parse", "main").stdout.strip()

    action, _fb, _muts = _run_merge(_merge_step(), ch, state, log)
    assert not isinstance(action, Pause)
    # No second merge commit on base.
    assert _git(tmp_path, "rev-parse", "main").stdout.strip() == base_before
    assert state.merge_commit == "deadbeef"


def test_route_merge_does_not_block_event_loop(tmp_path: Path, monkeypatch) -> None:
    """The merge step's git work (merge_to_base, under RepoGitLock) must run OFF
    the event loop, like a script gate. A conflict-heavy or large merge would
    otherwise starve concurrent tasks (status tick(), signal receiver,
    remote-channel poller) for its full duration. This stubs merge_to_base with a
    blocking sleep and asserts a concurrent ticker kept advancing.
    """
    import inspect
    import time
    import types

    import anyio

    from imp.engine import worktree
    from imp.engine.state import InMemoryEventLog

    def _blocking_merge(*a, **k):
        time.sleep(0.5)                     # stand-in for a slow git merge
        return worktree.MergeResult("MERGED", "deadbeef")

    monkeypatch.setattr(worktree, "merge_to_base", _blocking_merge)

    change = types.SimpleNamespace(
        change_dir=tmp_path, control_root=tmp_path, id="feat", handle="feat",
        base_branch="main", title="x", project_root=tmp_path, state_dir=tmp_path,
    )
    step = _merge_step()

    async def main() -> None:
        state = types.SimpleNamespace(vars={}, merge_commit=None,
                                      save=lambda *a, **k: None)
        log = InMemoryEventLog()

        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await anyio.sleep(0.02)
                ticks += 1

        async with anyio.create_task_group() as tg:
            tg.start_soon(ticker)
            await anyio.sleep(0.06)          # let the ticker spin up
            before = ticks
            res = _route_merge(step, change, state, log)
            if inspect.isawaitable(res):
                res = await res
            during = ticks - before
            tg.cancel_scope.cancel()

        assert during >= 8, (
            f"event loop starved during merge step: only {during} ticks advanced "
            "while a 0.5s merge ran (expected ~25)"
        )

    anyio.run(main)


def test_decisions_body_extracts_summary_section(tmp_path: Path) -> None:
    from imp.engine import executor
    spec = tmp_path / "spec.md"
    spec.write_text(
        "# Title\n\n## Goal\n\nDo the thing.\n\n"
        "## Decisions summary\n\n- D1 — picked A over B.\n- D2 — kept C.\n\n"
        "## Acceptance criteria\n\n- works\n")
    out = executor._decisions_body(spec)
    assert "D1 — picked A over B." in out
    assert "D2 — kept C." in out
    assert "Acceptance criteria" not in out   # stops at the next ## header
    assert "Do the thing." not in out         # only the Decisions section


def test_decisions_body_missing_section_returns_none(tmp_path: Path) -> None:
    from imp.engine import executor
    spec = tmp_path / "spec.md"
    spec.write_text("# Title\n\n## Goal\n\nDo the thing.\n")
    assert executor._decisions_body(spec) is None


def test_decisions_body_file_not_found_returns_none(tmp_path: Path) -> None:
    from imp.engine import executor
    assert executor._decisions_body(tmp_path / "nonexistent.md") is None

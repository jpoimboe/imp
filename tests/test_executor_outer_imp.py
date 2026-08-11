"""Run-start warning when the outer (running) imp is not the managed checkout's own
src. Covers the emission gating: once per quest per process, never duplicated by the
mid-run sandbox-policy rebuild, silent when the invariant holds."""
from __future__ import annotations

from pathlib import Path

import pytest

import imp
from imp.engine.executor import RunResult, run_quest
from imp.engine.quest import Quest
from imp.engine.sandbox import SandboxPolicy, _inactive
from imp.paths import project_state_dir
from tests.test_executor import (
    _register_fake,
    _register_flip,
    _seed_minimal_workflow,
    _seed_two_step_workflow,
)

_RUNNING_SRC = Path(imp.__file__).resolve().parent.parent


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _fake_imp_checkout(root: Path) -> None:
    """Make `root` look like an imp checkout whose src is NOT the running one."""
    (root / "src" / "imp").mkdir(parents=True)
    (root / "src" / "imp" / "__init__.py").write_text("")


def _outer_imp_events(events: list[dict]) -> list[dict]:
    return [e for e in events
            if e["type"] == "warning"
            and e.get("payload", {}).get("source") == "outer_imp"]


@pytest.mark.anyio
@pytest.mark.bare_sandbox
async def test_mismatched_outer_imp_warns_once_at_run_start(
        tmp_path: Path) -> None:
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    _fake_imp_checkout(tmp_path)
    _register_fake({"status": "Success"})

    events: list[dict] = []
    result = await run_quest(quest, on_event=events.append)

    assert result is RunResult.COMPLETED
    warnings = _outer_imp_events(events)
    assert len(warnings) == 1
    message = warnings[0]["payload"]["message"]
    assert message.startswith("outer imp mismatch: ")
    assert str(_RUNNING_SRC) in message
    assert str((tmp_path / "src").resolve()) in message
    assert warnings[0]["payload"]["detail"] == {}


@pytest.mark.anyio
async def test_project_root_flip_does_not_duplicate_the_warning(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Go-live flips quest.project_root mid-run and rebuilds the sandbox policy
    (executor.py: `if quest.project_root != policy_root`). The warning must not ride
    along with that rebuild.

    The rebuild is proven by the recorded build calls, not by reading
    quest.project_root afterwards: _complete calls _exit_worktree, which resets
    project_root to the control root before this test could observe the flip.

    The stub replaces the policy build wholesale, so a check misplaced inside
    _build_sandbox_policy fails this test with zero events rather than two —
    still a failure, just one that reads as "not emitted".
    """
    policy_builds: list[Path] = []

    def _counting_build(quest: Quest, config: object, log: object) -> SandboxPolicy:
        policy_builds.append(quest.project_root)
        return _inactive(note="executor_test_bare")

    # Supersedes conftest's autouse bwrap fallback (set later, so this one wins);
    # the marker is therefore not used here.
    import imp.engine.executor as ex
    monkeypatch.setattr(ex, "_build_sandbox_policy", _counting_build)

    quest = _seed_two_step_workflow(tmp_path)
    _fake_imp_checkout(tmp_path)
    wt = project_state_dir(tmp_path) / "worktrees" / quest.id
    wt.mkdir(parents=True)
    sink: list[str] = []          # _FlipRunner uses its length to flip on step 1 only
    _register_flip(sink, wt)

    events: list[dict] = []
    result = await run_quest(quest, on_event=events.append)

    assert result is RunResult.COMPLETED
    assert policy_builds == [tmp_path, wt]   # run start, then the go-live rebuild
    assert len(_outer_imp_events(events)) == 1


@pytest.mark.anyio
@pytest.mark.bare_sandbox
async def test_second_run_quest_entry_in_the_same_process_stays_silent(
        tmp_path: Path) -> None:
    """The daemon hold loop re-enters run_quest after every user reply; _SNAPSHOT_CAPTURED
    gates those later entries, so the warning is emitted at most once per process."""
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    _fake_imp_checkout(tmp_path)
    _register_fake({"status": "Success"})

    events: list[dict] = []
    await run_quest(quest, on_event=events.append)
    assert len(_outer_imp_events(events)) == 1

    from imp.engine.archive import read as archive_read
    archive_read.rehydrate_quest(quest.control_root, quest.id)  # home reaped on done
    before_second = len(events)
    await run_quest(quest, on_event=events.append)              # same process, 2nd entry

    # Liveness: the second entry really ran, so the count below is a gated
    # emission and not a run that bailed before the run-start block.
    assert len(events) > before_second
    assert len(_outer_imp_events(events)) == 1


@pytest.mark.anyio
@pytest.mark.bare_sandbox
async def test_matching_outer_imp_emits_nothing(
        tmp_path: Path) -> None:
    """The control root's src IS the running src (reached via symlink): no warning."""
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    (tmp_path / "src").symlink_to(_RUNNING_SRC, target_is_directory=True)
    _register_fake({"status": "Success"})

    events: list[dict] = []
    result = await run_quest(quest, on_event=events.append)

    assert result is RunResult.COMPLETED
    assert _outer_imp_events(events) == []


@pytest.mark.anyio
@pytest.mark.bare_sandbox
async def test_non_imp_project_emits_nothing(
        tmp_path: Path) -> None:
    quest, _, _ = _seed_minimal_workflow(tmp_path)   # ordinary project: no src/imp
    _register_fake({"status": "Success"})

    events: list[dict] = []
    result = await run_quest(quest, on_event=events.append)

    assert result is RunResult.COMPLETED
    assert _outer_imp_events(events) == []

"""A quest branch cannot rewrite the settings that judge it: the project layer is
the MAIN CHECKOUT's imp/config.yaml (spec §1). imp/memory/ stays worktree-scoped.

The one-step workflow is a script step that records the gate env it was handed,
so the assertion is on what the run actually shipped to a subprocess, not on a
loader return value. The workflow dir is named `default` and no layer sets
`default-workflow`, so workflow resolution never depends on the config layer
under test.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.executor import RunResult, run_quest
from imp.engine.quest import Quest
from imp.engine.state import State
from imp.paths import project_state_dir
from tests.conftest import _git, _init_repo
from tests.test_executor import _FAKE_CONFIG, _register_fake


@pytest.fixture
def anyio_backend():
    return "asyncio"


_RECORD_CMD = (
    "printf '%s|%s' \"$IMP_TEST_CMD_FAST\" \"$PYTHONPATH\""
    " > \"$IMP_PROJECT_ROOT/gate.txt\""
)

_WORKFLOW = (
    "name: t\ndescription: t\n"
    "defaults: {max_retries: 1, max_loops: 6}\n"
    "phases:\n"
    "  - name: only\n"
    "    steps:\n"
    "      - name: s\n"
    f"        command: {_RECORD_CMD}\n"
    "        rules:\n"
    "          - {else: true, pause: true}\n"
)


def _seed_control_root(root: Path) -> None:
    """A main checkout on `main`: committed settings naming the gate command,
    plus the recording workflow. `project_state_dir(root)/config.yaml` is the
    out-of-tree local layer."""
    _init_repo(root)
    wf = root / "imp" / "workflows" / "default"
    wf.mkdir(parents=True)
    (wf / "workflow.yaml").write_text(_WORKFLOW)
    (root / "imp" / "config.yaml").write_text(
        "test_cmd_fast: control-fast\npythonpath: [ctlsrc]\n")
    (project_state_dir(root) / "config.yaml").write_text(
        _FAKE_CONFIG + "sandbox:\n  enabled: false\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "imp setup")


def _recorded(work: Path) -> tuple[str, str]:
    fast, _, pypath = (work / "gate.txt").read_text().partition("|")
    return fast, pypath


@pytest.mark.anyio
async def test_a_worktree_config_cannot_change_the_settings_a_run_uses(
    tmp_path: Path,
) -> None:
    _seed_control_root(tmp_path)
    _register_fake({"status": "Success"})
    quest = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    work = quest.project_root
    (work / "imp" / "config.yaml").write_text(
        "test_cmd_fast: branch-fast\npythonpath: [branchsrc]\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "branch rewrites the settings that judge it")

    assert await run_quest(quest, on_event=lambda _e: None) is RunResult.PAUSED

    fast, pypath = _recorded(work)
    assert fast == "control-fast"
    assert str(work / "ctlsrc") in pypath
    assert "branchsrc" not in pypath


@pytest.mark.anyio
async def test_deleting_the_worktree_config_cannot_blind_the_gate(
    tmp_path: Path,
) -> None:
    _seed_control_root(tmp_path)
    _register_fake({"status": "Success"})
    quest = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    work = quest.project_root
    _git(work, "rm", "-q", "imp/config.yaml")
    _git(work, "commit", "-qm", "branch deletes its config")

    assert await run_quest(quest, on_event=lambda _e: None) is RunResult.PAUSED

    fast, pypath = _recorded(work)
    assert fast == "control-fast"
    assert str(work / "ctlsrc") in pypath


@pytest.mark.anyio
async def test_memory_is_still_frozen_from_the_worktree(tmp_path: Path) -> None:
    """Non-regression guard: memory is the ONE branch-scoped payload, so a note
    that exists only on the branch must still reach state.memory (AC 4b)."""
    _seed_control_root(tmp_path)
    _register_fake({"status": "Success"})
    quest = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    work = quest.project_root
    (work / "imp" / "memory").mkdir(parents=True)
    (work / "imp" / "memory" / "all.md").write_text("branch-only note")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "branch memory")

    assert await run_quest(quest, on_event=lambda _e: None) is RunResult.PAUSED

    assert State.load(quest.state_dir).memory == {"all.md": "branch-only note"}
    assert not (tmp_path / "imp" / "memory").exists()   # nothing in the checkout


def test_the_quest_less_subagent_fallback_reads_no_project_layer(
    tmp_path: Path,
) -> None:
    """AC 1(e): the fallback at subagents.py:310 passes control_root=None, so the
    workflow name comes from floor + global only — neither imp/config.yaml nor
    .imp/config.yaml under ctx.project_root is read. Pinned at the CALL SITE:
    every other subagent-dispatch test stubs the loader with `lambda **kw` and
    would not notice this argument changing back to a worktree root.

    conftest's autouse _isolate_global_config already points IMP_HOME at an
    absent dir, so there is no global layer either.
    """
    import anyio

    from imp.agents.subagents import dispatch_task
    from imp.assets import bundled_workflow_dir
    from tests.test_subagents import _dispatch_ctx, _spawn_yielding

    # Both layers under ctx.project_root name a DIFFERENT bundled workflow.
    (tmp_path / "imp").mkdir(parents=True, exist_ok=True)
    (tmp_path / "imp" / "config.yaml").write_text("default-workflow: minimal\n")
    (project_state_dir(tmp_path) / "config.yaml").write_text("default-workflow: minimal\n")

    ctx = _dispatch_ctx(tmp_path)
    ctx.quest_context = None          # the quest-less path that synthesizes one
    capture: dict = {}
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_result", "payload": {"text": "ok"}}], capture=capture)

    anyio.run(dispatch_task, ctx, "reviewer", "do x")

    # The floor default, NOT the `minimal` both project-root layers asked for.
    assert capture["inv"].quest_context.workflow_dir == bundled_workflow_dir("default")


def test_workflow_dirs_resolve_default_workflow_from_the_control_root(
    tmp_path: Path,
) -> None:
    """Both properties keep their own workflow-DIR root, but neither lets a
    worktree's imp/config.yaml pick the workflow NAME (AC 1d)."""
    _seed_control_root(tmp_path)
    quest = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    work = quest.project_root
    other = work / "imp" / "workflows" / "other"
    other.mkdir(parents=True)
    (other / "workflow.yaml").write_text(_WORKFLOW)
    (work / "imp" / "config.yaml").write_text("default-workflow: other\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "branch picks its own workflow")

    assert quest.live_workflow_dir == work / "imp" / "workflows" / "default"
    assert quest.control_workflow_dir == tmp_path / "imp" / "workflows" / "default"


@pytest.mark.anyio
async def test_a_worktree_on_the_pre_rename_name_is_neither_healed_nor_read(
    tmp_path: Path,
) -> None:
    """Spec §7 (AC 8a/8b): a branch forked before the imp/project.yaml ->
    imp/config.yaml rename used to be rewritten and committed at run entry. The
    project layer no longer comes from the worktree, so run entry leaves the
    branch exactly as it found it and the control root's settings hold anyway."""
    _seed_control_root(tmp_path)
    _register_fake({"status": "Success"})
    quest = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    work = quest.project_root
    _git(work, "mv", "imp/config.yaml", "imp/project.yaml")
    _git(work, "commit", "-qm", "branch predates the config rename")
    head_before = _git(work, "rev-parse", "HEAD").stdout

    events: list[dict] = []
    assert await run_quest(quest, on_event=events.append) is RunResult.PAUSED

    # The branch's own files are untouched: no rename, no commit on imp/feat.
    assert (work / "imp" / "project.yaml").is_file()
    assert not (work / "imp" / "config.yaml").exists()
    assert _git(work, "rev-parse", "HEAD").stdout == head_before
    assert not [e for e in events if e["type"] == "project_config_renamed"]
    # And the run was judged by the main checkout's settings regardless.
    fast, pypath = _recorded(work)
    assert fast == "control-fast"
    assert str(work / "ctlsrc") in pypath

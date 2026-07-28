"""Run entry heals a quest worktree branched before the imp/project.yaml ->
imp/config.yaml rename (spec scope 2). The live failure: `imp resume` died with
the transition guard because the quest's branch predates the rename."""
from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.errors import LoadError
from imp.engine.executor import RunResult, run_quest
from imp.engine.quest import Quest
from tests.conftest import _git, _seed_control
from tests.test_executor import _FAKE_CONFIG, _register_fake

_HEAL_SUBJECT = "chore: rename imp/project.yaml to imp/config.yaml"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _seed_pausing_control(root: Path) -> None:
    """A control root on `main` whose one-step workflow pauses after the step,
    so the run reaches its end with the worktree still in place. clean_tree is
    off: the dirty-preservation test deliberately leaves the tree dirty."""
    _seed_control(root)
    wf = root / "imp" / "workflows" / "imp"
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n")
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - name: w\n"
        "        file: agents/writer.md\n"
        "        tier: low\n"
        "        clean_tree: false\n"
        "        rules:\n"
        "          - {else: true, pause: true}\n")
    (root / "notes.txt").write_text("n\n")
    (root / ".imp" / "config.yaml").write_text(
        _FAKE_CONFIG + "sandbox:\n  enabled: false\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "pausing workflow")


@pytest.mark.anyio
async def test_run_entry_heals_a_worktree_still_on_the_old_config_name(
    tmp_path: Path,
) -> None:
    _seed_pausing_control(tmp_path)
    quest = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    work = quest.project_root
    # The quest's branch predates the rename: it carries the OLD name.
    _git(work, "mv", "imp/config.yaml", "imp/project.yaml")
    _git(work, "commit", "-qm", "pre-rename layout")
    _register_fake({"status": "Success"})
    # Unrelated work a dormant worktree may legitimately hold: one unstaged
    # edit, one staged edit.
    (work / "README.md").write_text("seed\nunstaged edit\n")
    (work / "notes.txt").write_text("staged edit\n")
    _git(work, "add", "notes.txt")
    status_before = _git(work, "status", "--porcelain").stdout

    events: list[dict] = []
    result = await run_quest(quest, on_event=events.append)

    # Without the heal this run does not pause — it dies in load_effective_config.
    assert result is RunResult.PAUSED
    assert (work / "imp" / "config.yaml").is_file()
    assert not (work / "imp" / "project.yaml").exists()
    assert _git(tmp_path, "log", "-1", "--format=%s", "imp/feat").stdout.strip() == (
        _HEAL_SUBJECT)
    assert _git(work, "show", "--format=", "--name-status", "-M", "HEAD").stdout == (
        "R100\timp/project.yaml\timp/config.yaml\n")
    # The dirty work survives byte-identical, in the same staged/unstaged state.
    assert _git(work, "status", "--porcelain").stdout == status_before
    assert (work / "README.md").read_text() == "seed\nunstaged edit\n"
    assert (work / "notes.txt").read_text() == "staged edit\n"
    assert [e["payload"] for e in events
            if e["type"] == "project_config_renamed"] == [{"branch": "imp/feat"}]


@pytest.mark.anyio
async def test_run_entry_with_nothing_to_heal_emits_no_event(tmp_path: Path) -> None:
    _seed_pausing_control(tmp_path)
    quest = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    _register_fake({"status": "Success"})
    head_before = _git(quest.project_root, "rev-parse", "HEAD").stdout

    events: list[dict] = []
    result = await run_quest(quest, on_event=events.append)

    assert result is RunResult.PAUSED
    assert not [e for e in events if e["type"] == "project_config_renamed"]
    assert _git(quest.project_root, "rev-parse", "HEAD").stdout == head_before


@pytest.mark.anyio
async def test_an_unmaterialized_draft_is_never_healed(tmp_path: Path) -> None:
    """project_root == control_root is the whole guard: healing a draft would
    commit on the user's own branch. The load raises as before instead — and
    run_quest re-raises it, because its last-resort handler runs before the
    event log is bound."""
    _seed_pausing_control(tmp_path)
    draft = Quest.create_draft(
        tmp_path, title="x", quest_id="draft", base_branch="main")
    assert draft.project_root == draft.control_root
    _git(tmp_path, "mv", "imp/config.yaml", "imp/project.yaml")
    _git(tmp_path, "commit", "-qm", "user's own staged rename, not yet renamed")
    head_before = _git(tmp_path, "rev-parse", "HEAD").stdout

    with pytest.raises(LoadError) as exc:
        await run_quest(draft, on_event=lambda _e: None)

    assert str(exc.value) == (
        f"imp/project.yaml found in {tmp_path} (branch main) but imp now reads "
        f"imp/config.yaml — rename it with "
        f"`git -C {tmp_path} mv imp/project.yaml imp/config.yaml`")
    assert _git(tmp_path, "rev-parse", "HEAD").stdout == head_before
    assert (tmp_path / "imp" / "project.yaml").is_file()
    assert not (tmp_path / "imp" / "config.yaml").exists()

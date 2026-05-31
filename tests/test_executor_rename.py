# tests/test_executor_rename.py
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event
from imp.engine import worktree as wt
from imp.engine.change import Change
from imp.engine.executor import RunResult, apply_pending_rename, run_change
from imp.engine.state import State
from tests.conftest import _git, _seed_control


# ── Unit tests: apply_pending_rename ────────────────────────────────────────


def test_apply_pending_rename_repoints_paths(tmp_path: Path) -> None:
    import json
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", slug="old", base_branch="main")
    created_before = json.loads((ch.change_dir / "change.json").read_text())["created_at"]
    # H1: the rename rests on `git worktree move` relocating the gitignored
    # .imp/change/ intact. Write a sentinel event + state value BEFORE the move.
    ch.event_log().append(agent_id=None, type="sentinel", payload={"k": "v"})
    state = State.load(ch.state_dir)
    state.vars["marker"] = "keep-me"
    state.pending_rename = "Add OAuth"
    state.save(ch.state_dir)
    log = ch.event_log()

    new_log = apply_pending_rename(ch, state, log, on_event=None)
    assert ch.slug == "add-oauth"
    assert ch.project_root == (tmp_path / ".imp" / "worktrees" / "add-oauth").resolve()
    assert (ch.change_dir / "change.json").exists()
    assert state.pending_rename is None
    types = [e["type"] for e in new_log.read()]
    assert "sentinel" in types and "change_renamed" in types
    assert State.load(ch.state_dir).vars["marker"] == "keep-me"
    meta_after = json.loads((ch.change_dir / "change.json").read_text())
    assert meta_after["created_at"] == created_before
    assert meta_after["slug"] == "add-oauth" and meta_after["base_branch"] == "main"


def test_apply_pending_rename_collision_keeps_old(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", slug="old", base_branch="main")
    wt.create_worktree(tmp_path, "taken", "main")  # imp/taken exists
    state = State.load(ch.state_dir)
    state.pending_rename = "taken"
    returned = apply_pending_rename(ch, state, ch.event_log(), on_event=None)
    assert ch.slug == "old"  # unchanged
    assert state.pending_rename is None
    assert "rename_blocked" in [e["type"] for e in returned.read()]


# ── Integration tests: run_change with rename + cwd restore ─────────────────


class _FakeRunner:
    def __init__(self, output: dict) -> None:
        self._output = output

    async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
        yield {"type": "agent_output", "payload": self._output}

    async def interrupt(self) -> None:
        pass


class _FakeProvider:
    name = "anthropic"

    def __init__(self, model: str = "m") -> None:
        self.model = model

    def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
        return _FakeRunner({"status": "Success"})


def _seed_two_step_control(root: Path) -> None:
    """Like _seed_control but a TWO-step workflow so there's a boundary between
    step 1 and step 2 where a pending rename is applied mid-run."""
    from tests.conftest import _init_repo, write_agent_md

    _init_repo(root)
    src = root / "imp" / "workflows" / "default"
    (src / "agents").mkdir(parents=True)
    for stem in ("a", "b"):
        write_agent_md(
            src / "agents" / f"{stem}.md",
            frontmatter=(
                f"name: {stem}\ntools: [complete_step]\n"
                "output:\n  status: {type: enum, values: [Success, Error]}"
            ),
        )
    (src / "workflow.yaml").write_text(
        "name: t\ndescription: \"\"\n"
        "phases:\n  - name: p\n    steps:\n"
        "      - {name: s1, file: agents/a.md}\n"
        "      - {name: s2, file: agents/b.md}\n"
    )
    (root / "imp" / "config.yaml").write_text(
        "roles:\n  default: {prefer: [{provider: anthropic, model: x}]}\n"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "imp setup")


def test_run_change_applies_rename_and_restores_cwd(
    tmp_path: Path, provider_registry
) -> None:
    provider_registry("anthropic", lambda: True, lambda model: _FakeProvider(model))
    _seed_two_step_control(tmp_path)
    ch = Change.create(tmp_path, title="x", slug="old", base_branch="main")
    st = State.load(ch.state_dir)
    st.pending_rename = "Renamed Thing"
    st.save(ch.state_dir)

    cwd_before = os.getcwd()
    result = anyio.run(run_change, ch)
    assert result is RunResult.COMPLETED
    assert os.getcwd() == cwd_before                       # 4b: cwd restored
    assert ch.slug == "renamed-thing"                      # 4c: rename applied mid-run
    assert (tmp_path / ".imp" / "worktrees" / "renamed-thing").is_dir()
    assert not (tmp_path / ".imp" / "worktrees" / "old").exists()


def test_run_change_restores_cwd_on_error(tmp_path: Path, provider_registry) -> None:
    provider_registry("anthropic", lambda: True, lambda model: _FakeProvider(model))
    _seed_two_step_control(tmp_path)
    ch = Change.create(tmp_path, title="x", slug="boom", base_branch="main")
    (ch.workflow_dir / "workflow.yaml").write_text("}{ not yaml")  # loader raises mid-run

    cwd_before = os.getcwd()
    with pytest.raises(Exception):
        anyio.run(run_change, ch)
    assert os.getcwd() == cwd_before                       # restored on error path


@pytest.fixture
def anyio_backend():
    return "asyncio"

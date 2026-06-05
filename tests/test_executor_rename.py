# tests/test_executor_rename.py
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event
from imp.engine.change import Change
from imp.engine.errors import LoadError
from imp.engine.executor import RunResult, apply_pending_rename, run_change
from imp.engine.state import State
from tests.conftest import _git, _seed_control


def test_apply_pending_rename_is_content_only(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="orig",
                       change_id="2026-06-04-aaa111", base_branch="main")
    created_before = json.loads((ch.change_dir / "change.json").read_text())["created_at"]
    id_before, root_before = ch.id, ch.project_root
    ch.event_log().append(agent_id=None, type="sentinel", payload={"k": "v"})
    state = State.load(ch.state_dir)
    state.pending_rename = "Add OAuth"
    state.save(ch.state_dir)

    new_log = apply_pending_rename(ch, state, ch.event_log(), on_event=None)

    assert ch.title == "Add OAuth"
    assert ch.slug == "2026-06-04-add-oauth"
    assert ch.id == id_before                       # id never changes
    assert ch.project_root == root_before           # no worktree move
    assert state.pending_rename is None
    meta = json.loads((ch.change_dir / "change.json").read_text())
    assert meta["title"] == "Add OAuth"
    assert meta["created_at"] == created_before
    assert "id" in meta and "slug" not in meta
    types = [e["type"] for e in new_log.read()]
    assert "sentinel" in types and "change_renamed" in types
    renamed = [e for e in new_log.read() if e["type"] == "change_renamed"][-1]
    assert renamed["payload"] == {
        "id": id_before, "old_title": "orig", "new_title": "Add OAuth"}


def test_apply_pending_rename_moves_committed_spec(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="orig",
                       change_id="2026-06-04-bbb222", base_branch="main")
    specs = ch.project_root / "imp" / "specs"
    specs.mkdir(parents=True)
    old = specs / f"{ch.slug}.md"
    old.write_text("# spec")
    _git(ch.project_root, "add", "-A")
    _git(ch.project_root, "commit", "-qm", "early spec")
    old_slug = ch.slug

    state = State.load(ch.state_dir)
    state.pending_rename = "Renamed"
    state.save(ch.state_dir)
    apply_pending_rename(ch, state, ch.event_log(), on_event=None)

    assert not (specs / f"{old_slug}.md").exists()
    assert (specs / f"{ch.slug}.md").exists()


def test_apply_pending_rename_moves_untracked_draft(tmp_path: Path) -> None:
    # An UNTRACKED early draft must still be moved (filesystem fallback when
    # `git mv` fails), or it lingers and blocks the merge. [review M2]
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="orig",
                       change_id="2026-06-04-ggg777", base_branch="main")
    specs = ch.project_root / "imp" / "specs"
    specs.mkdir(parents=True)
    old_slug = ch.slug
    (specs / f"{old_slug}.md").write_text("# draft")   # never git-added

    state = State.load(ch.state_dir)
    state.pending_rename = "Renamed"
    state.save(ch.state_dir)
    apply_pending_rename(ch, state, ch.event_log(), on_event=None)

    assert not (specs / f"{old_slug}.md").exists()      # nothing lingers at old name
    assert (specs / f"{ch.slug}.md").exists()           # moved to settled name


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
        "providers: [anthropic]\n"
        "tiers:\n"
        "  low: {anthropic: x}\n"
        "  medium: {anthropic: x}\n"
        "  high: {anthropic: x}\n"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "imp setup")


def test_run_change_applies_rename_keeps_id_and_path(
    tmp_path: Path, provider_registry
) -> None:
    provider_registry("anthropic", lambda: True, lambda model: _FakeProvider(model))
    _seed_two_step_control(tmp_path)
    ch = Change.create(tmp_path, title="x",
                       change_id="2026-06-04-ccc333", base_branch="main")
    st = State.load(ch.state_dir)
    st.pending_rename = "Renamed Thing"
    st.save(ch.state_dir)

    cwd_before = os.getcwd()
    result = anyio.run(run_change, ch)
    assert result is RunResult.COMPLETED
    assert os.getcwd() == cwd_before
    assert ch.title == "Renamed Thing"
    assert ch.id == "2026-06-04-ccc333"                          # id stable
    assert (tmp_path / ".imp" / "worktrees" / "2026-06-04-ccc333").is_dir()


def test_run_change_restores_cwd_on_error(tmp_path: Path, provider_registry) -> None:
    provider_registry("anthropic", lambda: True, lambda model: _FakeProvider(model))
    _seed_two_step_control(tmp_path)
    ch = Change.create(tmp_path, title="x",
                       change_id="2026-06-04-ddd444", base_branch="main")
    (ch.workflow_dir / "workflow.yaml").write_text("}{ not yaml")

    cwd_before = os.getcwd()
    with pytest.raises(LoadError):
        anyio.run(run_change, ch)
    assert os.getcwd() == cwd_before


@pytest.fixture
def anyio_backend():
    return "asyncio"

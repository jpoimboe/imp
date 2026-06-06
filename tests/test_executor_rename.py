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
from imp.engine.executor import RunResult, run_change
from tests.conftest import _git


def test_change_rename_is_content_only(tmp_path: Path) -> None:
    from tests.conftest import _seed_control
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="orig",
                       change_id="2026-06-04-aaa111", base_branch="main")
    created_before = json.loads(
        (ch.change_dir / "change.json").read_text())["created_at"]
    id_before, root_before = ch.id, ch.project_root

    new_slug = ch.rename("Add OAuth", log=ch.event_log())

    assert new_slug == "2026-06-04-add-oauth"
    assert ch.title == "Add OAuth"
    assert ch.id == id_before                       # id never changes
    assert ch.project_root == root_before           # no worktree move
    meta = json.loads((ch.change_dir / "change.json").read_text())
    assert meta["title"] == "Add OAuth"
    assert meta["created_at"] == created_before
    assert "id" in meta and "slug" not in meta
    renamed = [e for e in ch.event_log().read() if e["type"] == "change_renamed"][-1]
    assert renamed["payload"] == {
        "id": id_before, "old_title": "orig", "new_title": "Add OAuth"}


class _RenamingRunner:
    """Fake runner: on its single turn, calls Change.rename via the tool path by
    mutating through the invocation's live change, then returns Success."""

    def __init__(self, invocation: AgentInvocation) -> None:
        self._inv = invocation

    async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
        ch = invocation.change
        if ch is not None and ch.title == "x":
            ch.rename("Renamed Thing", log=invocation.event_log or ch.event_log())
        yield {"type": "agent_output", "payload": {"status": "Success"}}

    async def interrupt(self) -> None:
        pass


class _FakeProvider:
    name = "anthropic"

    def __init__(self, model: str = "m") -> None:
        self.model = model

    def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
        return _RenamingRunner(invocation)


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


def test_run_change_rename_keeps_id_and_path(
    tmp_path: Path, provider_registry
) -> None:
    provider_registry("anthropic", lambda: True, lambda model: _FakeProvider(model))
    _seed_two_step_control(tmp_path)
    ch = Change.create(tmp_path, title="x",
                       change_id="2026-06-04-ccc333", base_branch="main")

    cwd_before = os.getcwd()
    result = anyio.run(run_change, ch)
    assert result is RunResult.COMPLETED
    assert os.getcwd() == cwd_before
    # Reload to confirm the rename persisted to change.json (the in-memory ch is
    # the same object the runner mutated; reloading proves durability).
    reloaded = Change.load(tmp_path, "2026-06-04-ccc333")
    assert reloaded.title == "Renamed Thing"
    assert reloaded.id == "2026-06-04-ccc333"
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

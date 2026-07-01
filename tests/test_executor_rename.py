# tests/test_executor_rename.py
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event, Message, TextBlock
from imp.agents.transcript import Transcript
from imp.engine.change import Change
from imp.engine.errors import LoadError
from imp.engine.executor import RunResult, run_change
from imp.engine.state import State
from tests.conftest import _git


def test_change_rename_is_content_only(tmp_path: Path) -> None:
    from tests.conftest import _seed_control
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="orig",
                       change_id="aaa111", base_branch="main")
    created_before = json.loads(
        (ch.change_dir / "change.json").read_text())["created_at"]
    id_before, root_before = ch.id, ch.project_root

    returned = ch.rename("Add OAuth", log=ch.event_log())

    assert returned == "aaa111"                     # rename returns the id
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
        yield {"type": "agent_output", "payload": {"outcome": "ok", "status": "Success"}}

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
    src = root / "imp" / "workflows" / "imp"
    (src / "agents").mkdir(parents=True)
    for stem in ("a", "b"):
        write_agent_md(
            src / "agents" / f"{stem}.md",
            frontmatter=(
                f"name: {stem}\ntools: [complete_step]\n"
                "output:\n  status: {type: enum, values: [Success, Error]}"
            ),
            body="{% if run.opening %}OPENING:{{ run.opening }}{% endif %} do it",
        )
    (src / "workflow.yaml").write_text(
        "name: t\ndescription: \"\"\n"
        "phases:\n  - name: p\n    steps:\n"
        "      - {name: s1, file: agents/a.md}\n"
        "      - {name: s2, file: agents/b.md}\n"
    )
    _cfg = (
        "providers: [anthropic]\n"
        "tiers:\n"
        "  low: {anthropic: x}\n"
        "  medium: {anthropic: x}\n"
        "  high: {anthropic: x}\n"
    )
    # Config layers: committed marker + gitignored local layer.
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (root / ".imp").mkdir(exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(_cfg)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "imp setup")


def test_run_change_rename_keeps_id_and_path(
    tmp_path: Path, provider_registry
) -> None:
    provider_registry("anthropic", lambda: True, lambda model: _FakeProvider(model))
    _seed_two_step_control(tmp_path)
    ch = Change.create(tmp_path, title="x",
                       change_id="ccc333", base_branch="main")

    cwd_before = os.getcwd()
    result = anyio.run(run_change, ch)
    assert result is RunResult.COMPLETED
    assert os.getcwd() == cwd_before
    # Reload to confirm the rename persisted to change.json (the in-memory ch is
    # the same object the runner mutated; reloading proves durability).
    reloaded = Change.load(tmp_path, "ccc333")
    assert reloaded.title == "Renamed Thing"
    assert reloaded.id == "ccc333"
    assert (tmp_path / ".imp" / "worktrees" / "ccc333").is_dir()


def test_run_change_restores_cwd_on_error(tmp_path: Path, provider_registry) -> None:
    provider_registry("anthropic", lambda: True, lambda model: _FakeProvider(model))
    _seed_two_step_control(tmp_path)
    ch = Change.create(tmp_path, title="x",
                       change_id="ddd444", base_branch="main")
    (ch.workflow_dir / "workflow.yaml").write_text("}{ not yaml")

    cwd_before = os.getcwd()
    with pytest.raises(LoadError):
        anyio.run(run_change, ch)
    assert os.getcwd() == cwd_before



class _RecordingRunner:
    """Fake runner: records the invocation's opening (initial_messages) keyed by
    agent_id, then completes without ever calling wait_for_user."""

    def __init__(self, sink: dict[str, Any]) -> None:
        self._sink = sink

    async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
        self._sink[invocation.agent_id] = invocation.initial_messages
        yield {"type": "agent_output", "payload": {"outcome": "ok", "status": "Success"}}

    async def interrupt(self) -> None:
        pass


class _RecordingProvider:
    name = "anthropic"

    def __init__(self, sink: dict[str, Any], model: str = "m") -> None:
        self.model = model
        self._sink = sink

    def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
        return _RecordingRunner(self._sink)


@pytest.mark.anyio
async def test_seed_opening_first_step_only(tmp_path: Path, provider_registry) -> None:
    sink: dict[str, Any] = {}
    provider_registry("anthropic", lambda: True,
                      lambda model: _RecordingProvider(sink, model))
    _seed_two_step_control(tmp_path)
    ch = Change.create(tmp_path, title="",
                       change_id="aaa111", base_branch="main")

    events: list[dict[str, Any]] = []
    result = await run_change(
        ch, on_event=events.append,
        seed_opening="Implement this spec X", non_interactive=True)

    assert result is RunResult.COMPLETED
    s1 = sink["step:p/s1"][0].content[0].text
    s2 = sink["step:p/s2"][0].content[0].text
    assert "OPENING:Implement this spec X" in s1     # intent rendered on step 1
    assert "OPENING:" not in s2                       # one-shot: not on step 2
    assert "<imp:begin/>" not in s1 and "<imp:begin/>" not in s2
    assert "complete_step" in s1                      # completion contract rides along
    assert not any(e["type"] == "awaiting_user_input" for e in events)


@pytest.mark.anyio
async def test_run_change_none_seed_opening_has_empty_opening(
    tmp_path: Path, provider_registry
) -> None:
    sink: dict[str, Any] = {}
    provider_registry("anthropic", lambda: True,
                      lambda model: _RecordingProvider(sink, model))
    _seed_two_step_control(tmp_path)
    ch = Change.create(tmp_path, title="",
                       change_id="bbb222", base_branch="main")

    result = await run_change(
        ch, on_event=lambda _e: None, seed_opening=None, non_interactive=True)

    assert result is RunResult.COMPLETED
    s1 = sink["step:p/s1"][0].content[0].text
    assert "OPENING:" not in s1                       # run.opening empty -> bare
    assert "<imp:begin/>" not in s1
    assert "complete_step" in s1


@pytest.mark.anyio
async def test_seed_opening_forwarded_to_next_fresh_step_on_resume(
    tmp_path: Path, provider_registry
) -> None:
    sink: dict[str, Any] = {}
    provider_registry("anthropic", lambda: True,
                      lambda model: _RecordingProvider(sink, model))
    _seed_two_step_control(tmp_path)
    ch = Change.create(tmp_path, title="",
                       change_id="ccc333", base_branch="main")

    state = State.load(ch.state_dir)
    state.resume_pending = state.step  # simulate a paused-then-resumed first step
    state.save(ch.state_dir)
    # A genuinely paused-then-resumed step actually ran: it has a non-empty,
    # replayable transcript and a stored step_context. (An empty transcript is a
    # never-ran step, which now correctly re-runs fresh instead of replaying an
    # empty query — see _resume_replays_transcript.)
    s1_dir = ch.change_dir / "steps" / "p" / "s1"
    s1_dir.mkdir(parents=True, exist_ok=True)
    Transcript(s1_dir / "transcript.jsonl").append(
        Message(role="user", content=(TextBlock(text="prior turn"),)))
    ch.event_log().append_step_context(
        agent_id="step:p/s1", provider="anthropic", model="m",
        system_prompt="sys", tools_schema=[], params={}, max_tool_calls=None)

    result = await run_change(
        ch, on_event=lambda _e: None,
        seed_opening="Implement this spec SHOULD NOT APPEAR",
        non_interactive=True)

    assert result is RunResult.COMPLETED
    # On a resumed step the executor passes initial_messages=() — no seed injected
    assert sink["step:p/s1"] == ()
    assert "Implement this spec SHOULD NOT APPEAR" in sink["step:p/s2"][0].content[0].text


@pytest.fixture
def anyio_backend():
    return "asyncio"

from __future__ import annotations

import contextlib
import importlib
import json
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event
from imp.agents.subagents import SubagentSuspended
from imp.engine.change import Change
from imp.engine.executor import RunResult, run_change
from imp.engine.state import State


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize("provider_mod, runner_cls", [
    ("imp.providers.anthropic", "AnthropicAgentRunner"),
    ("imp.providers.openai", "OpenAIAgentRunner"),
])
@pytest.mark.anyio
async def test_provider_run_reraises_subagent_suspended(provider_mod, runner_cls) -> None:
    """Each generator-based provider's outer run() must PROPAGATE
    SubagentSuspended, not swallow it into a classified error event.
    (Both runners share `__init__(model_name, *, client_factory=None,
    user_input_fn=None)`; _run_loop is overridden so the client is never used.)"""
    base = getattr(importlib.import_module(provider_mod), runner_cls)

    class _Runner(base):  # type: ignore[misc,valid-type]
        async def _run_loop(self, invocation):
            raise SubagentSuspended("nested pause")
            yield  # makes this an async generator

    runner = _Runner(model_name="m")
    inv = object.__new__(AgentInvocation)  # fields unused; _run_loop is stubbed
    with pytest.raises(SubagentSuspended):
        async for _ in runner.run(inv):
            pass


def _e2e_agent_def(name, *, tools, subagents=()):
    from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
    return AgentDef(
        name=name, description="", tools=frozenset(tools),
        subagents=tuple(subagents),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1)),
        prompt_template_source="go", source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1))


@pytest.mark.anyio
async def test_pause_propagates_through_dispatch_task_and_task_wrapper(tmp_path) -> None:
    from imp.agents.runner import ChangeContext, Message, TextBlock
    from imp.cli.signals import InterruptCoordinator
    from imp.providers import resolve
    from imp.providers.anthropic import AnthropicAgentRunner
    from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use

    resolve.reset_providers_for_tests()
    coord = InterruptCoordinator()

    class _SubRunner:
        async def run(self, inv):
            coord._should_pause = True       # simulate ESC mid-subagent
            yield {"type": "thinking", "payload": {}}
        async def interrupt(self) -> None: ...

    class _SubProvider:
        name = "fake"
        model = "m"
        def create_runner(self, _inv):
            return _SubRunner()

    resolve.register_provider("fake", env_check=lambda: True,
                              factory=lambda _m: _SubProvider())

    # Parent: a real anthropic runner whose first turn calls `task`.
    fake = FakeAsyncAnthropic([response(
        tool_use("t1", "task",
                 {"subagent_name": "reviewer", "task_description": "review"}))])
    runner = AnthropicAgentRunner(model_name="m", client_factory=lambda: fake)

    from imp.agents.runner import AgentInvocation
    from imp.engine.types import FsRule, SourceLoc
    inv = AgentInvocation(
        agent_def=_e2e_agent_def("lead", tools={"complete_step", "task"},
                                 subagents=("reviewer",)),
        provider_name="fake", model_name="m",        # subagent inherits -> fake
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(slug="t", title="T", project_root=tmp_path,
                                     workflow_dir=tmp_path / "wf"),
        interactive=False, transcript_dir=tmp_path / "step",
        fs_permissions=(FsRule(operations=("read",), paths=("/project/**",),
                               mode="allow",
                               source=SourceLoc(file=Path("w.yaml"), line=1)),),
        events_path=tmp_path / "events.jsonl",
        subagent_defs={"reviewer": _e2e_agent_def("reviewer",
                                                  tools={"complete_step"})},
        agent_id="step:p/s", coordinator=coord)

    # The pause unwinds as an exception out of run(), NOT an is_error event.
    with pytest.raises(SubagentSuspended):
        async for _ in runner.run(inv):
            pass


def _seed_single_step(root: Path) -> Change:
    from imp.engine.loader import load_workflow
    from imp.providers.config import load_config

    wf = root / ".imp" / "workflows" / "default"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n")
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 3, max_loops: 6, on_max_retries: {pause: true}}\n"
        "phases:\n  - name: only\n    steps:\n"
        "      - {name: w, file: agents/writer.md, model_role: default}\n")
    (root / "imp").mkdir(parents=True, exist_ok=True)
    (root / "imp" / "config.yaml").write_text(
        "roles:\n  default:\n    prefer: [{provider: fake, model: m}]\n")

    change_dir = root / ".imp" / "change"
    change_dir.mkdir(parents=True, exist_ok=True)
    dest_wf = change_dir / "workflow"
    if dest_wf.exists():
        shutil.rmtree(dest_wf)
    shutil.copytree(wf, dest_wf)
    (change_dir / "state").mkdir(exist_ok=True)
    (change_dir / "change.json").write_text(json.dumps(
        {"slug": "s", "title": "t", "base_branch": "main"}, indent=2, sort_keys=True))
    configured: list[str] = []
    with contextlib.suppress(Exception):
        configured = load_config(root).role_names()
    workflow = load_workflow(dest_wf, configured_roles=configured)
    flat = workflow.flat_steps()
    State(slug="s", step=flat[0].name, step_attempt=0, status="running",
          vars=dict(workflow.vars.defaults()), loop_counts={}).save(change_dir / "state")
    change = Change(slug="s", title="t", base_branch="main", control_root=root,
                    project_root=root, change_dir=change_dir)
    change.event_log().append(agent_id=None, type="change_created",
                              payload={"slug": "s", "title": "t"})
    return change


@pytest.mark.anyio
async def test_subagent_pause_unwinds_then_resume_restarts(tmp_path: Path) -> None:
    """A SubagentSuspended raised inside the step runner unwinds to
    status=paused; a resume re-enters the step fresh and completes."""
    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    state = {"first": True}

    class _Runner:
        async def run(self, invocation) -> AsyncIterator[Event]:
            if state["first"]:
                state["first"] = False
                raise SubagentSuspended("paused inside subagent")
                yield  # async generator
            yield {"type": "agent_output", "payload": {"status": "Success"}}
        async def interrupt(self) -> None: ...

    runner = _Runner()

    class _Provider:
        name = "fake"
        model = "m"
        def create_runner(self, _inv) -> AgentRunner:
            return runner

    resolve.register_provider("fake", env_check=lambda: True,
                              factory=lambda _m: _Provider())

    change = _seed_single_step(tmp_path)
    r1 = await run_change(change)
    assert r1 is RunResult.PAUSED
    st = State.load(change.state_dir)
    assert st.status == "paused"
    assert st.step == "w"  # cursor unchanged -> resume restarts the step

    r2 = await run_change(change)
    assert r2 is RunResult.COMPLETED

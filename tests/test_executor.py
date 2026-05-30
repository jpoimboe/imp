# tests/test_executor.py
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event
from imp.engine.change import Change
from imp.engine.executor import RunResult, run_change
from imp.engine.state import State


class _FakeRunner:
    def __init__(self, output: dict) -> None:
        self._output = output

    async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
        yield {"type": "agent_output", "payload": self._output}

    async def interrupt(self) -> None:
        pass


class _FakeProvider:
    name = "fake"

    def __init__(self, output: dict, model: str = "m") -> None:
        self._output = output
        self.model = model

    def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
        return _FakeRunner(self._output)


def _register_fake(output: dict) -> None:
    from imp.providers import resolve
    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "fake", env_check=lambda: True,
        factory=lambda model: _FakeProvider(output, model=model),
    )


def _seed_minimal_workflow(
    root: Path,
    *,
    phase_name: str = "only",
    step_name: str = "w",
    agent_stem: str = "writer",
) -> tuple[Change, str, str]:
    """Create a minimal single-step workflow on disk and snapshot it into a
    Change. Returns ``(change, phase_name, step_name)``."""
    wf_dir = root / ".imp" / "workflows" / "default"
    (wf_dir / "agents").mkdir(parents=True)
    (wf_dir / "agents" / f"{agent_stem}.md").write_text(
        f"---\nname: {agent_stem}\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\n"
        "go {{ change.id }}\n"
    )
    (wf_dir / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n"
        f"  - name: {phase_name}\n"
        "    steps:\n"
        f"      - {{name: {step_name}, file: agents/{agent_stem}.md, model_role: default}}\n"
    )
    config = root / ".imp" / "config.yaml"
    config.write_text(
        "roles:\n  default:\n    prefer:\n      - {provider: fake, model: m}\n"
    )
    change = Change.create(root, title="t")
    return change, phase_name, step_name


# ── Minimal completion + transcript lifecycle ───────────────────────────────


@pytest.mark.anyio
async def test_run_change_completes_minimal_workflow(tmp_path: Path) -> None:
    change, _, _ = _seed_minimal_workflow(tmp_path)
    _register_fake({"status": "Success"})

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED

    state = State.load(change.state_dir)
    assert state.status == "completed"
    assert state.step is None


@pytest.mark.anyio
async def test_continue_off_last_step_completes(tmp_path: Path) -> None:
    """A single agent step with no rules → continue → COMPLETED, and the
    steps/ tree is removed at completion."""
    change, _, _ = _seed_minimal_workflow(tmp_path)
    _register_fake({"status": "Success"})

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED
    assert not (change.change_dir / "steps").exists()


# ── set: mutation persistence ───────────────────────────────────────────────


@pytest.mark.anyio
async def test_set_mutation_persists_to_state_vars(tmp_path: Path) -> None:
    """A rule with `set: {review_done: true}` updates state.vars; reload State
    and confirm the mutation persisted."""
    wf = tmp_path / ".imp" / "workflows" / "default"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "vars: {review_done: {type: bool, default: false}}\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - name: w\n"
        "        file: agents/writer.md\n"
        "        model_role: default\n"
        "        rules:\n"
        "          - {else: true, continue: true, set: {review_done: true}}\n"
    )
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n  default:\n    prefer:\n      - {provider: fake, model: m}\n"
    )
    change = Change.create(tmp_path, title="t")
    _register_fake({"status": "Success"})

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED

    state = State.load(change.state_dir)
    assert state.vars["review_done"] is True


# ── branch routing on a var ─────────────────────────────────────────────────


def _seed_branch_loop_workflow(root: Path) -> Change:
    """writer(agent, context: resume) sets review_done=true; a branch step
    continues while review_done is false, and gotos the trailing `done` phase
    once it's true."""
    wf = root / ".imp" / "workflows" / "default"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "vars: {review_done: {type: bool, default: false}}\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n"
        "  - name: work\n"
        "    steps:\n"
        "      - name: writer\n"
        "        file: agents/writer.md\n"
        "        model_role: default\n"
        "        context: resume\n"
        "        rules:\n"
        "          - {else: true, continue: true, set: {review_done: true}}\n"
        "      - name: route\n"
        "        rules:\n"
        "          - {if: 'review_done', goto: done}\n"
        "          - {else: true, goto: writer}\n"
        "  - name: done\n"
        "    steps: []\n"
    )
    (root / ".imp" / "config.yaml").write_text(
        "roles:\n  default:\n    prefer:\n      - {provider: fake, model: m}\n"
    )
    return Change.create(root, title="t")


@pytest.mark.anyio
async def test_branch_routes_on_var(tmp_path: Path) -> None:
    change = _seed_branch_loop_workflow(tmp_path)
    _register_fake({"status": "Success"})

    events: list[dict] = []
    result = await run_change(change, on_event=events.append)
    assert result is RunResult.COMPLETED

    # The branch step's step_started payload reports kind == "branch".
    branch_started = [
        e for e in events
        if e["type"] == "step_started"
        and e["payload"].get("step") == "route"
    ]
    assert branch_started, "branch step never started"
    assert branch_started[0]["payload"]["kind"] == "branch"


# ── backward goto loop counting + max_loops trip ────────────────────────────


@pytest.mark.anyio
async def test_backward_goto_increments_loop_count_and_trips_max_loops(
    tmp_path: Path,
) -> None:
    """A reviewer agent that always gotos backward to the writer, with
    max_loops: 2 → after exhausting, the run PAUSES and loop_counts reflects
    the cap."""
    wf = tmp_path / ".imp" / "workflows" / "default"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n"
    )
    (wf / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "  verdict: {type: enum, values: [approve, rework]}\n---\ngo\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 10}\n"
        "phases:\n"
        "  - name: work\n"
        "    steps:\n"
        "      - {name: writer, file: agents/writer.md, model_role: default}\n"
        "      - name: reviewer\n"
        "        file: agents/reviewer.md\n"
        "        model_role: default\n"
        "        max_loops: 2\n"
        "        rules:\n"
        "          - {if: {verdict: rework}, goto: writer, feedback: false}\n"
        "          - {else: true, continue: true}\n"
    )
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n  default:\n    prefer:\n      - {provider: fake, model: m}\n"
    )
    change = Change.create(tmp_path, title="t")

    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    class _Runner:
        def __init__(self, name: str) -> None:
            self._name = name

        async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
            if self._name == "reviewer":
                yield {"type": "agent_output",
                       "payload": {"status": "Success", "verdict": "rework"}}
            else:
                yield {"type": "agent_output", "payload": {"status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _Provider:
        name = "fake"
        model = "m"

        def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
            return _Runner(invocation.agent_def.name)

    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda _m: _Provider(),
    )

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.PAUSED

    state = State.load(change.state_dir)
    assert state.status == "paused"
    # max_loops: 2 → loop trips on the 3rd backward goto (n == 3 > 2).
    assert state.loop_counts["reviewer"] == 3


# ── goto into an empty trailing phase completes ─────────────────────────────


@pytest.mark.anyio
async def test_goto_empty_trailing_phase_completes(tmp_path: Path) -> None:
    """The last real step gotos a trailing empty phase → COMPLETED."""
    wf = tmp_path / ".imp" / "workflows" / "default"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n"
        "  - name: work\n"
        "    steps:\n"
        "      - name: w\n"
        "        file: agents/writer.md\n"
        "        model_role: default\n"
        "        rules:\n"
        "          - {else: true, goto: done}\n"
        "  - name: done\n"
        "    steps: []\n"
    )
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n  default:\n    prefer:\n      - {provider: fake, model: m}\n"
    )
    change = Change.create(tmp_path, title="t")
    _register_fake({"status": "Success"})

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED
    assert State.load(change.state_dir).step is None


# ── step_completed / step_started payload shapes ────────────────────────────


@pytest.mark.anyio
async def test_step_completed_payload_has_action_dict_and_kind(
    tmp_path: Path,
) -> None:
    """step_completed.payload.action == {"verb": "continue"} and the agent
    step_started.payload carries all of {step, kind, phase}."""
    change, phase, step = _seed_minimal_workflow(
        tmp_path, phase_name="myphase", step_name="mystep", agent_stem="writer",
    )
    _register_fake({"status": "Success"})

    events: list[dict] = []
    result = await run_change(change, on_event=events.append)
    assert result is RunResult.COMPLETED

    started = next(
        e for e in events
        if e["type"] == "step_started" and e["payload"].get("step") == step
    )
    for key in ("step", "kind", "phase"):
        assert key in started["payload"], key
    assert started["payload"]["kind"] == "agent"
    assert started["payload"]["phase"] == phase

    completed = next(e for e in events if e["type"] == "step_completed")
    assert completed["payload"]["action"] == {"verb": "continue"}
    assert completed["payload"]["kind"] == "agent"


# ── agent_id chaining ───────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_step_events_carry_step_agent_id_and_step_started_seeds_chain(
    tmp_path: Path,
) -> None:
    change, phase, step = _seed_minimal_workflow(
        tmp_path, phase_name="myphase", step_name="mystep", agent_stem="worker",
    )
    _register_fake({"status": "Success"})

    await run_change(change, on_event=lambda _e: None)
    events = list(change.event_log().read())
    started = next(e for e in events if e["type"] == "step_started")
    output = next(e for e in events if e["type"] == "agent_output")
    assert started["agent_id"] is None
    assert output["agent_id"] == f"step:{phase}/{step}"


# ── error handling: fatal / auth / rate-limit / incomplete ──────────────────


class _FatalToolErrorRunner:
    def __init__(self, *, tool: str = "task", message: str = "boom") -> None:
        self._tool = tool
        self._message = message
        self.run_count = 0

    async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
        self.run_count += 1
        yield {"type": "error", "payload": {
            "kind": "fatal_tool_error",
            "tool": self._tool,
            "message": self._message,
        }}

    async def interrupt(self) -> None:
        pass


@pytest.mark.anyio
async def test_fatal_tool_error_pauses_without_retry(tmp_path: Path) -> None:
    change, _, _ = _seed_minimal_workflow(tmp_path)

    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    runner = _FatalToolErrorRunner(
        tool="task",
        message="subagent task spawn requested but no spawn fn installed",
    )

    class _FatalProvider:
        name = "fake"
        model = "m"

        def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
            return runner

    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda _m: _FatalProvider(),
    )

    events: list[dict] = []
    result = await run_change(change, on_event=events.append)
    assert result is RunResult.PAUSED
    assert State.load(change.state_dir).status == "paused"
    assert runner.run_count == 1

    fatal_events = [e for e in events
                    if e["type"] == "error"
                    and e.get("payload", {}).get("kind") == "fatal_tool_error"]
    assert len(fatal_events) == 1
    assert fatal_events[0]["payload"]["tool"] == "task"


class _CategoryErrorRunner:
    def __init__(self, *, category: str, kind: str, message: str) -> None:
        self._category = category
        self._kind = kind
        self._message = message
        self.run_count = 0

    async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
        self.run_count += 1
        yield {"type": "error", "payload": {
            "category": self._category,
            "kind": self._kind,
            "message": self._message,
        }}

    async def interrupt(self) -> None:
        pass


@pytest.mark.anyio
async def test_auth_error_pauses_without_burning_retries(tmp_path: Path) -> None:
    change, _, _ = _seed_minimal_workflow(tmp_path)

    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    runner = _CategoryErrorRunner(
        category="auth", kind="AuthError",
        message="Invalid API key — Anthropic returned 401",
    )

    class _AuthProvider:
        name = "fake"
        model = "m"

        def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
            return runner

    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda _m: _AuthProvider(),
    )

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.PAUSED
    assert runner.run_count == 1
    assert State.load(change.state_dir).status == "paused"


class _RateLimitThenSuccessRunner:
    def __init__(self, output: dict) -> None:
        self._output = output
        self.run_count = 0

    async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
        self.run_count += 1
        if self.run_count == 1:
            yield {"type": "error", "payload": {
                "category": "rate_limit",
                "kind": "RateLimitError",
                "message": "429 Too Many Requests",
            }}
            return
        yield {"type": "agent_output", "payload": self._output}

    async def interrupt(self) -> None:
        pass


@pytest.mark.anyio
async def test_rate_limit_sleeps_then_retries_without_consuming_step_attempt(
    tmp_path: Path, monkeypatch,
) -> None:
    from imp.engine import error_policy
    monkeypatch.setattr(error_policy, "_BACKOFF_SCHEDULE", [0.0, 0.0, 0.0, 0.0])

    change, _, _ = _seed_minimal_workflow(tmp_path)
    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    runner = _RateLimitThenSuccessRunner({"status": "Success"})

    class _RLProvider:
        name = "fake"
        model = "m"

        def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
            return runner

    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda _m: _RLProvider(),
    )

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED
    assert runner.run_count == 2

    state = State.load(change.state_dir)
    assert state.step_attempt == 0


@pytest.mark.anyio
async def test_rate_limit_exhaustion_pauses(tmp_path: Path, monkeypatch) -> None:
    from imp.engine import error_policy
    monkeypatch.setattr(error_policy, "_BACKOFF_SCHEDULE", [0.0, 0.0, 0.0, 0.0])

    change, _, _ = _seed_minimal_workflow(tmp_path)
    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    runner = _CategoryErrorRunner(
        category="rate_limit", kind="RateLimitError", message="429",
    )

    class _RLProvider:
        name = "fake"
        model = "m"

        def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
            return runner

    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda _m: _RLProvider(),
    )

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.PAUSED
    assert runner.run_count == 5


@pytest.mark.anyio
async def test_agent_incomplete_error_pauses(tmp_path: Path) -> None:
    """A runner that emits an agent_incomplete error makes the step Pause
    (fatal), not retry."""
    change, _, _ = _seed_minimal_workflow(tmp_path)

    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    class _IncompleteRunner:
        async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
            yield {"type": "error", "payload": {
                "category": "agent_incomplete",
                "kind": "no_complete_step_after_nudges",
                "message": "x",
            }}

        async def interrupt(self) -> None:
            pass

    class _IncompleteProvider:
        name = "fake"
        model = "m"

        def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
            return _IncompleteRunner()

    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda _m: _IncompleteProvider(),
    )

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.PAUSED


# ── retries exhaust into on_max_retries (Pause) ─────────────────────────────


@pytest.mark.anyio
async def test_agent_missing_output_exhausts_to_pause(tmp_path: Path) -> None:
    """A runner that never yields agent_output exhausts max_retries (1) and
    pauses via on_max_retries (default Pause)."""
    change, _, _ = _seed_minimal_workflow(tmp_path)

    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    runner_calls = {"n": 0}

    class _EmptyRunner:
        async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
            runner_calls["n"] += 1
            if False:
                yield {}  # pragma: no cover — make this an async generator

        async def interrupt(self) -> None:
            pass

    class _EmptyProvider:
        name = "fake"
        model = "m"

        def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
            return _EmptyRunner()

    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda _m: _EmptyProvider(),
    )

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.PAUSED
    # max_retries=1 → initial attempt + 1 retry = 2 invocations before pause.
    assert runner_calls["n"] == 2
    assert State.load(change.state_dir).status == "paused"


# ── completion instruction + continuation wiring ────────────────────────────


@pytest.mark.anyio
async def test_system_prompt_carries_completion_instruction_and_continuation(
    tmp_path: Path,
) -> None:
    from imp.providers import resolve

    wf = tmp_path / ".imp" / "workflows" / "default"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n"
        "  status: {type: enum, values: [Success, Error]}\n"
        "  verdict: {type: enum, values: [approve, rework]}\n---\n"
        "go\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n"
        "  - name: a\n"
        "    steps:\n"
        "      - {name: w, file: agents/writer.md, model_role: default}\n"
    )
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n  default:\n    prefer:\n      - {provider: fake, model: m}\n"
    )
    change = Change.create(tmp_path, title="t")

    captured: dict[str, AgentInvocation] = {}

    class _CapRunner:
        async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
            captured["inv"] = invocation
            yield {"type": "agent_output",
                   "payload": {"status": "Success", "verdict": "approve"}}

        async def interrupt(self) -> None:
            pass

    class _CapProvider:
        name = "fake"

        def __init__(self, model: str = "m") -> None:
            self.model = model

        def create_runner(self, invocation: AgentInvocation) -> _CapRunner:
            return _CapRunner()

    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda model: _CapProvider(model),
    )

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED

    inv = captured["inv"]
    assert "complete_step" in inv.system_prompt
    for token in ("status", "Success", "Error", "verdict", "approve", "rework"):
        assert token in inv.system_prompt, token
    assert inv.continuation_fn is not None
    assert inv.continuation_fn("prose") is not None   # nudge 1
    assert inv.continuation_fn("prose") is not None   # nudge 2
    assert inv.continuation_fn("prose") is None        # exhausted (N=2)


@pytest.mark.anyio
async def test_interactive_end_of_turn_emits_awaiting_user_input(
    tmp_path: Path,
) -> None:
    import inspect as _inspect

    from imp.providers import resolve

    wf = tmp_path / ".imp" / "workflows" / "default"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "asker.md").write_text(
        "---\nname: asker\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\n"
        "ask\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n"
        "  - name: a\n"
        "    steps:\n"
        "      - {name: w, file: agents/asker.md, model_role: default, interactive: true}\n"
    )
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n  default:\n    prefer:\n      - {provider: fake, model: m}\n"
    )
    change = Change.create(tmp_path, title="t")

    events: list[dict] = []

    class _AskRunner:
        async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
            reply = invocation.continuation_fn("What outcome do you want?")
            if _inspect.isawaitable(reply):
                reply = await reply
            assert reply == "sure, go ahead"
            yield {"type": "agent_output", "payload": {"status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _AskProvider:
        name = "fake"

        def __init__(self, model: str = "m") -> None:
            self.model = model

        def create_runner(self, invocation: AgentInvocation) -> _AskRunner:
            return _AskRunner()

    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda model: _AskProvider(model),
    )

    def _chat(_q: str) -> str:
        assert any(e.get("type") == "awaiting_user_input" for e in events), (
            "awaiting_user_input must be emitted before user_chat_input_fn reads"
        )
        return "sure, go ahead"

    result = await run_change(
        change, on_event=events.append, user_chat_input_fn=_chat,
    )
    assert result is RunResult.COMPLETED

    awaiting = [e for e in events if e.get("type") == "awaiting_user_input"]
    assert len(awaiting) == 1, [e.get("type") for e in events]
    assert awaiting[0]["payload"]["prompt"] == "What outcome do you want?"


# ── agent-rule goto feeds back to the resumed target ────────────────────────


@pytest.mark.anyio
async def test_goto_feedback_resumes_target_with_wrapper(tmp_path: Path) -> None:
    """A reviewer agent that gotos backward (feedback: true) to a context:resume
    coder makes the coder's NEXT invocation carry the reviewer's feedback
    string. The reviewer flips to approve on its 2nd pass so the run completes."""
    wf = tmp_path / ".imp" / "workflows" / "default"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "coder.md").write_text(
        "---\nname: coder\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n"
    )
    (wf / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "  verdict: {type: enum, values: [Accept, Reject]}\n---\ngo\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n"
        "  - name: impl\n"
        "    steps:\n"
        "      - {name: coder, file: agents/coder.md, model_role: default, context: resume}\n"
        "      - name: reviewer\n"
        "        file: agents/reviewer.md\n"
        "        model_role: default\n"
        "        rules:\n"
        "          - {if: {verdict: Reject}, goto: coder}\n"
        "          - {else: true, continue: true}\n"
    )
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n  default:\n    prefer:\n      - {provider: fake, model: m}\n"
    )
    change = Change.create(tmp_path, title="t")

    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    runs: list[tuple[str, str | None]] = []   # (agent_id, retry_feedback)
    reviewer_calls = {"n": 0}

    class _Runner:
        async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
            aid = invocation.agent_id
            runs.append((aid, invocation.retry_feedback))
            if invocation.agent_def.name == "reviewer":
                reviewer_calls["n"] += 1
                verdict = "Reject" if reviewer_calls["n"] == 1 else "Accept"
                yield {"type": "agent_output",
                       "payload": {"status": "Success", "verdict": verdict}}
            else:
                yield {"type": "agent_output", "payload": {"status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _Provider:
        name = "fake"
        model = "m"

        def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
            return _Runner()

    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda _m: _Provider(),
    )

    events: list[dict] = []
    result = await run_change(change, on_event=events.append)
    assert result is RunResult.COMPLETED

    coder_runs = [fb for (aid, fb) in runs if aid.endswith("/coder")]
    assert len(coder_runs) == 2
    assert coder_runs[0] is None
    assert coder_runs[1] is not None and "reviewer" in coder_runs[1]

    # Feedback consumed: cleared on the engine's state.
    assert State.load(change.state_dir).retry_feedback is None

    # No agent_output_missing surfaced anywhere.
    assert not any(
        e["type"] == "error"
        and (e.get("payload") or {}).get("category") == "agent_output_missing"
        for e in events
    )


def test_action_payload_goto_carries_resume_bool() -> None:
    from imp.engine.executor import _action_payload
    from imp.engine.types import Continue, Goto
    assert _action_payload(Goto("coder", resume=True)) == {
        "verb": "goto", "target": "coder", "resume": True}
    assert _action_payload(Goto("coder")) == {
        "verb": "goto", "target": "coder", "resume": False}
    # non-goto actions are unchanged
    assert _action_payload(Continue()) == {"verb": "continue"}


@pytest.fixture
def anyio_backend():
    return "asyncio"

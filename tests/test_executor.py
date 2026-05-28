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


def _seed_minimal_workflow(
    root: Path,
    *,
    phase_name: str = "only",
    step_name: str = "w",
    agent_stem: str = "writer",
) -> tuple[Change, str, str]:
    """Create a minimal workflow on disk and snapshot it into a Change.

    Returns ``(change, phase_name, step_name)`` so callers that need to
    construct expected agent_ids can read the names back rather than hardcode.
    """
    wf_dir = root / ".imp" / "workflows" / "default"
    (wf_dir / "agents").mkdir(parents=True)
    (wf_dir / "agents" / f"{agent_stem}.md").write_text(
        f"---\nname: {agent_stem}\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\n"
        "go {{ change.id }}\n"
    )
    (wf_dir / "workflow.yaml").write_text(
        f"name: t\ndescription: t\nphases: [{phase_name}]\n"
        "defaults: {max_retries: 1, on_max_retries: {kind: pause}}\n"
        f"{phase_name}:\n  steps:\n"
        f"    - {{name: {step_name}, file: agents/{agent_stem}.md, model_role: default}}\n"
    )
    config = root / ".imp" / "config.yaml"
    config.write_text(
        "roles:\n  default:\n    prefer:\n      - {provider: fake, model: m}\n"
    )
    change = Change.create(root, title="t")
    return change, phase_name, step_name


@pytest.mark.anyio
async def test_run_change_completes_minimal_workflow(tmp_path: Path, monkeypatch) -> None:
    change, _, _ = _seed_minimal_workflow(tmp_path)

    from imp.providers import resolve
    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "fake", env_check=lambda: True,
        factory=lambda model: _FakeProvider({"status": "Success"}, model=model),
    )

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED

    state = State.load(change.state_dir)
    assert state.status == "completed"


@pytest.mark.anyio
async def test_step_level_human_gate_uses_renderer(tmp_path: Path) -> None:
    """A step-level human gate must render through the renderer's
    human_output_fn, not directly to print."""
    from imp.providers import resolve

    # Workflow with a step-level human gate (gate inside the step).
    wf = tmp_path / ".imp" / "workflows" / "default"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\n"
        "go\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\nphases: [only]\n"
        "defaults: {max_retries: 1, on_max_retries: {kind: pause}}\n"
        "only:\n"
        "  steps:\n"
        "    - name: w\n"
        "      file: agents/writer.md\n"
        "      model_role: default\n"
        "      gates:\n"
        "        - type: human\n"
        "          prompt: ok?\n"
        "          options: [{label: Yes, action: {kind: continue}}]\n"
    )
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n  default:\n    prefer: [{provider: fake, model: m}]\n"
    )
    change = Change.create(tmp_path, title="t")

    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "fake", env_check=lambda: True,
        factory=lambda model: _FakeProvider({"status": "Success"}, model=model),
    )

    captured: list[str] = []
    result = await run_change(
        change,
        on_event=lambda _e: None,
        human_input_fn=lambda: "1",
        human_output_fn=lambda msg: captured.append(msg),
    )
    assert result is RunResult.COMPLETED
    # The "─── HUMAN GATE ───" prefix must have flowed through the captured fn.
    assert any("HUMAN GATE" in line for line in captured)


@pytest.mark.anyio
async def test_human_gate_pending_emitted_once_before_action(
    tmp_path: Path,
) -> None:
    """`human_gate_pending` is emitted exactly once per gate invocation,
    immediately before `gate_evaluated`. Re-prompts on invalid input do not
    re-emit."""
    from imp.engine.change import Change
    from imp.engine.executor import run_change

    wf = tmp_path / ".imp" / "workflows" / "default"
    (wf / "agents").mkdir(parents=True)
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\nphases: [only]\n"
        "defaults: {max_retries: 1, on_max_retries: {kind: pause}}\n"
        "only:\n"
        "  steps: []\n"
        "  gates:\n"
        "    - type: human\n"
        "      prompt: Continue?\n"
        "      options:\n"
        "        - {label: Yes, action: {kind: continue}}\n"
    )
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n  default:\n    prefer: [{provider: fake, model: m}]\n"
    )
    change = Change.create(tmp_path, title="t")

    # Drive the human gate: first an empty answer (re-prompt), then "1".
    answers = iter(["", "1"])
    events: list[dict] = []

    await run_change(
        change,
        on_event=events.append,
        human_input_fn=lambda: next(answers),
        human_output_fn=lambda _msg: None,
    )

    pendings = [e for e in events if e["type"] == "human_gate_pending"]
    assert len(pendings) == 1, (
        f"expected 1 human_gate_pending event, got {len(pendings)}; "
        f"all types: {[e['type'] for e in events]}"
    )
    pending_idx = events.index(pendings[0])
    gate_eval_idx = next(
        i for i, e in enumerate(events)
        if e["type"] == "gate_evaluated" and e["payload"].get("kind") == "human"
    )
    assert pending_idx < gate_eval_idx
    payload = pendings[0]["payload"]
    assert payload["phase"] == "only"
    assert payload["prompt"] == "Continue?"
    assert payload["options"] == [{"label": "Yes"}]


class _FatalToolErrorRunner:
    """Yields a single fatal_tool_error event then stops, simulating a
    provider that caught FatalToolError from a tool invocation."""

    def __init__(self, *, tool: str = "ask_user", message: str = "boom") -> None:
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
async def test_fatal_tool_error_pauses_without_retry(
    tmp_path: Path,
) -> None:
    """A runner-emitted error with kind='fatal_tool_error' must halt the
    workflow immediately (status=paused) without burning step retries.

    Concretely: the step in _seed_minimal_workflow has max_retries=1.
    Old behavior treated all errors as retryable, so the runner.run()
    would fire twice before pause-on-max-retries kicked in. With fatal
    classification it fires exactly once."""
    change, _, _ = _seed_minimal_workflow(tmp_path)

    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    runner = _FatalToolErrorRunner(
        tool="ask_user",
        message="ask_user invoked but user_input_fn is not installed",
    )

    class _FatalProvider:
        name = "fake"
        model = "m"

        def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
            return runner

    resolve.register_provider(
        "fake", env_check=lambda: True,
        factory=lambda _m: _FatalProvider(),
    )

    events: list[dict] = []
    result = await run_change(change, on_event=events.append)
    assert result is RunResult.PAUSED

    state = State.load(change.state_dir)
    assert state.status == "paused"

    # runner.run() was invoked exactly once: no retry on fatal errors.
    assert runner.run_count == 1, (
        f"fatal_tool_error should not retry; ran {runner.run_count} times"
    )

    # The fatal_tool_error event is in the log (audit trail).
    fatal_events = [e for e in events
                    if e["type"] == "error"
                    and e.get("payload", {}).get("kind") == "fatal_tool_error"]
    assert len(fatal_events) == 1
    assert fatal_events[0]["payload"]["tool"] == "ask_user"


class _CategoryErrorRunner:
    """Yields a single classified error event then stops. Mimics a
    provider that classified an SDK exception and emitted the
    category-tagged error event."""

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
async def test_auth_error_pauses_without_burning_retries(
    tmp_path: Path,
) -> None:
    """A category=auth error must Pause immediately, identical to the
    legacy fatal_tool_error path but driven by the new policy."""
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
    assert runner.run_count == 1, (
        f"auth error should not retry; ran {runner.run_count} times"
    )

    state = State.load(change.state_dir)
    assert state.status == "paused"


class _RateLimitThenSuccessRunner:
    """First call yields a rate_limit error; subsequent calls succeed."""

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
    """A rate_limit error must trigger a backoff sleep + retry that does
    NOT increment state.step_attempt. After the first sleep, the second
    invocation returns agent_output and the workflow completes."""
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
    assert runner.run_count == 2, (
        f"expected exactly 2 invocations (1 rate-limit + 1 success); "
        f"got {runner.run_count}"
    )

    state = State.load(change.state_dir)
    assert state.step_attempt == 0


@pytest.mark.anyio
async def test_rate_limit_exhaustion_pauses(
    tmp_path: Path, monkeypatch,
) -> None:
    """4 consecutive rate_limit events → Pause (schedule exhausted)."""
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
    assert runner.run_count == 5, (
        f"expected 5 invocations (4 rate-limited with backoff + 1 final "
        f"where decide() exhausts the schedule and returns Pause); "
        f"got {runner.run_count}"
    )


@pytest.mark.anyio
async def test_agent_gate_rate_limit_backs_off_without_phase_attempt(
    tmp_path: Path, monkeypatch,
) -> None:
    """Same backoff property in the gate path: a rate_limit error from
    the agent-gate runner must sleep + retry without incrementing
    state.phase_attempt."""
    from imp.engine import error_policy
    monkeypatch.setattr(error_policy, "_BACKOFF_SCHEDULE", [0.0, 0.0, 0.0, 0.0])

    # Workflow with one trivial step + one agent-gate. The step runner
    # always succeeds; the gate runner rate-limits once then succeeds.
    wf = tmp_path / ".imp" / "workflows" / "default"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n"
    )
    (wf / "agents" / "gater.md").write_text(
        "---\nname: gater\ntools: [complete_step]\n"
        "output:\n"
        "  status: {type: enum, values: [Success, Error]}\n"
        "  verdict: {type: enum, values: [Yes, No]}\n---\ngo\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\nphases: [only]\n"
        "defaults: {max_retries: 1, on_max_retries: {kind: pause}}\n"
        "only:\n"
        "  steps:\n"
        "    - {name: w, file: agents/writer.md, model_role: default}\n"
        "  gates:\n"
        "    - type: agent\n"
        "      file: agents/gater.md\n"
        "      model_role: default\n"
        "      decide:\n"
        "        - {when: {verdict: Yes}, action: {kind: continue}}\n"
        "        - {else: true, action: {kind: retry}}\n"
    )
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n  default:\n    prefer: [{provider: fake, model: m}]\n"
    )
    change = Change.create(tmp_path, title="t")

    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    gate_calls = [0]

    class _AlternatingRunner:
        """Step path: always succeed.
        Gate path: 1st call rate-limits, 2nd call yields verdict=Yes."""

        def __init__(self, *, kind: str) -> None:
            self._kind = kind

        async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
            if self._kind == "gater":
                gate_calls[0] += 1
                if gate_calls[0] == 1:
                    yield {"type": "error", "payload": {
                        "category": "rate_limit",
                        "kind": "RateLimitError",
                        "message": "429",
                    }}
                    return
                yield {"type": "agent_output", "payload": {"status": "Success", "verdict": "Yes"}}
                return
            yield {"type": "agent_output", "payload": {"status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _AltProvider:
        name = "fake"
        model = "m"

        def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
            return _AlternatingRunner(kind=invocation.agent_def.name)

    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda _m: _AltProvider(),
    )

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED
    assert gate_calls[0] == 2, (
        f"expected 2 gate invocations (rate-limit then success); got {gate_calls[0]}"
    )

    state = State.load(change.state_dir)
    assert state.phase_attempt == 0


@pytest.mark.anyio
async def test_step_events_carry_step_agent_id_and_step_started_seeds_chain(
    tmp_path: Path,
) -> None:
    change, phase, step = _seed_minimal_workflow(
        tmp_path, phase_name="myphase", step_name="mystep", agent_stem="worker",
    )

    from imp.providers import resolve
    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "fake", env_check=lambda: True,
        factory=lambda model: _FakeProvider({"status": "Success"}, model=model),
    )

    await run_change(change, on_event=lambda _e: None)
    events = list(change.event_log().read())
    started = next(e for e in events if e["type"] == "step_started")
    output = next(e for e in events if e["type"] == "agent_output")
    assert started["agent_id"] is None
    assert output["agent_id"] == f"step:{phase}/{step}"


def test_gate_dir_allocator_never_reclaims_wiped_n(tmp_path: Path) -> None:
    from imp.engine.executor import _next_gate_invocation_dir
    gates = tmp_path / "gates"
    d1 = _next_gate_invocation_dir(gates, "lint")
    d1.mkdir(parents=True)
    assert d1.name == "lint-1"
    # Simulate success-wipe of lint-1.
    import shutil
    shutil.rmtree(d1)
    d2 = _next_gate_invocation_dir(gates, "lint")
    assert d2.name == "lint-2"            # NOT lint-1 again


@pytest.fixture
def anyio_backend():
    return "asyncio"

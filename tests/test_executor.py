# tests/test_executor.py
from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event
from imp.agents.transcript import Transcript
from imp.engine.change import Change
from imp.engine.executor import RunResult, run_change
from imp.engine.state import State


def _persist_initial(invocation: AgentInvocation) -> None:
    """Mimic the real providers' fresh-entry persistence (claude_code.py: the
    empty-transcript branch appends initial_messages to transcript.jsonl AND
    emits step_context). Fake runners must do the same so a later `resume:` entry
    sees a non-empty, replayable transcript with its stored system prompt —
    without it the resume either looks like a never-ran step (and re-runs fresh)
    or trips the resume invariant (non-empty transcript, no stored step_context).
    Only fresh entries carry initial_messages, so this no-ops on resume."""
    if not invocation.initial_messages:
        return
    t = Transcript(invocation.transcript_dir / "transcript.jsonl")
    for m in invocation.initial_messages:
        t.append(m)
    if invocation.event_log is not None:
        invocation.event_log.append_step_context(
            agent_id=invocation.agent_id,
            parent_id_override=invocation.first_event_parent_id,
            provider=invocation.provider_name, model="m",
            system_prompt=invocation.system_prompt,
            tools_schema=[], params={}, max_tool_calls=None)


def _make_change(root: Path, *, slug: str = "test-change", title: str = "t") -> Change:
    """Construct a Change directly on disk without git worktrees.

    The test sets up the workflow under root/imp/workflows/imp BEFORE
    calling _make_change; this helper seeds the State so run_change can be
    invoked immediately (no snapshot copy needed — workflow_dir resolves to
    root/imp/workflows/imp directly).
    """
    from imp.engine.loader import load_workflow
    from imp.providers.effective import load_effective_config

    src_wf = root / "imp" / "workflows" / "imp"
    change_dir = root / ".imp" / "change"
    change_dir.mkdir(parents=True, exist_ok=True)
    (change_dir / "state").mkdir(exist_ok=True)

    # Write change metadata
    (change_dir / "change.json").write_text(json.dumps({
        "id": slug, "title": title, "base_branch": "main",
    }, indent=2, sort_keys=True))

    configured_tiers: list[str] = []
    project_keys: frozenset[str] = frozenset()
    with contextlib.suppress(Exception):
        _cfg = load_effective_config(
            project_root=root, control_root=root).config
        configured_tiers = _cfg.tier_names()
        project_keys = frozenset(_cfg.project_facts)

    workflow = load_workflow(
        src_wf, configured_tiers=configured_tiers, project_keys=project_keys)
    flat = workflow.flat_steps()
    from imp.engine.state import State as _State
    _State(
        id=slug,
        step=flat[0].name if flat else None,
        step_attempt=0,
        status="running",
        vars=dict(workflow.vars.defaults()),
        loop_counts={},
    ).save(change_dir / "state")

    change = Change(
        id=slug, title=title, base_branch="main",
        control_root=root, project_root=root, change_dir=change_dir,
    )
    change.event_log().append(
        agent_id=None, type="change_created",
        payload={"id": slug, "handle": change.handle, "title": title},
    )
    return change


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


class _CapturingRunner:
    def __init__(self, output: dict, sink: list[str]) -> None:
        self._output = output
        self._sink = sink

    async def run(self, invocation: AgentInvocation) -> AsyncGenerator[Event, None]:
        self._sink.append(invocation.system_prompt)
        yield {"type": "agent_output", "payload": self._output}

    async def interrupt(self) -> None:
        pass


class _CapturingProvider:
    name = "fake"

    def __init__(self, output: dict, sink: list[str], model: str = "m") -> None:
        self._output = output
        self._sink = sink
        self.model = model

    def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
        return _CapturingRunner(self._output, self._sink)


def _register_prompt_capturing(output: dict, sink: list[str]) -> None:
    from imp.providers import resolve
    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "fake", env_check=lambda: True,
        factory=lambda model: _CapturingProvider(output, sink, model=model),
    )


_FAKE_CONFIG = (
    "providers: [fake]\ntiers:\n  low: {fake: m}\n  medium: {fake: m}\n  high: {fake: m}\n"
)


def _write_fake_config(root: Path) -> None:
    """Write the `fake`-provider config in the layered layout that
    load_effective_config reads: imp/project.yaml marker + .imp/config.yaml
    local layer."""
    (root / "imp").mkdir(parents=True, exist_ok=True)
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (root / ".imp").mkdir(parents=True, exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(_FAKE_CONFIG)


def _seed_minimal_workflow(
    root: Path,
    *,
    phase_name: str = "only",
    step_name: str = "w",
    agent_stem: str = "writer",
) -> tuple[Change, str, str]:
    """Create a minimal single-step workflow on disk and snapshot it into a
    Change. Returns ``(change, phase_name, step_name)``."""
    wf_dir = root / "imp" / "workflows" / "imp"
    (wf_dir / "agents").mkdir(parents=True)
    (wf_dir / "agents" / f"{agent_stem}.md").write_text(
        f"---\nname: {agent_stem}\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\n"
        "go {{ change.handle }}\n"
    )
    (wf_dir / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n"
        f"  - name: {phase_name}\n"
        "    steps:\n"
        f"      - {{name: {step_name}, file: agents/{agent_stem}.md, tier: medium}}\n"
    )
    _write_fake_config(root)
    change = _make_change(root)
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
    wf = tmp_path / "imp" / "workflows" / "imp"
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
        "        tier: medium\n"
        "        rules:\n"
        "          - {else: true, continue: true, set: {review_done: true}}\n"
    )
    _write_fake_config(tmp_path)
    change = _make_change(tmp_path)
    _register_fake({"status": "Success"})

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED

    state = State.load(change.state_dir)
    assert state.vars["review_done"] is True


# ── branch routing on a var ─────────────────────────────────────────────────


def _seed_branch_loop_workflow(root: Path) -> Change:
    """writer(agent); the branch loops back via resume: writer. writer sets
    review_done=true; a branch step continues while review_done is false, and
    gotos the trailing `done` phase once it's true."""
    wf = root / "imp" / "workflows" / "imp"
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
        "        tier: medium\n"
        "        rules:\n"
        "          - {else: true, continue: true, set: {review_done: true}}\n"
        "      - name: route\n"
        "        rules:\n"
        "          - {if: 'review_done', goto: done}\n"
        "          - {else: true, resume: writer}\n"
        "  - name: done\n"
        "    steps: []\n"
    )
    _write_fake_config(root)
    return _make_change(root)


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
    wf = tmp_path / "imp" / "workflows" / "imp"
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
        "      - {name: writer, file: agents/writer.md, tier: medium}\n"
        "      - name: reviewer\n"
        "        file: agents/reviewer.md\n"
        "        tier: medium\n"
        "        max_loops: 2\n"
        "        rules:\n"
        "          - {if: {verdict: rework}, goto: writer}\n"
        "          - {else: true, continue: true}\n"
    )
    _write_fake_config(tmp_path)
    change = _make_change(tmp_path)

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
    wf = tmp_path / "imp" / "workflows" / "imp"
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
        "        tier: medium\n"
        "        rules:\n"
        "          - {else: true, goto: done}\n"
        "  - name: done\n"
        "    steps: []\n"
    )
    _write_fake_config(tmp_path)
    change = _make_change(tmp_path)
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
    from imp.engine import executor
    # §10: no _BACKOFF_SCHEDULE; patch the interruptible sleep to be instant.
    async def _instant_sleep(delay, coordinator, *, tick=0.1):  # type: ignore[misc]
        pass
    monkeypatch.setattr(executor, "_interruptible_sleep", _instant_sleep)

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
async def test_headless_retry_ceiling_pauses_non_interactive(
    tmp_path: Path, monkeypatch,
) -> None:
    """§10/§12: non_interactive run hits the wall-clock ceiling and pauses cleanly."""
    from imp.engine import executor
    # Patch ceiling to a tiny value so the first retry delay (1.0s) exceeds it.
    monkeypatch.setattr(executor, "_HEADLESS_RETRY_CEILING_SECONDS", 0.5)
    # Patch sleep to be instant so the test is fast.
    async def _instant_sleep(delay, coordinator, *, tick=0.1):  # type: ignore[misc]
        pass
    monkeypatch.setattr(executor, "_interruptible_sleep", _instant_sleep)

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

    events: list[dict] = []
    result = await run_change(change, on_event=events.append, non_interactive=True)
    assert result is RunResult.PAUSED
    # Should pause after the first attempt (ceiling exceeded before retry)
    assert runner.run_count == 1
    # The pause reason should be retry_ceiling
    paused_events = [e for e in events if e["type"] == "workflow_paused"]
    assert paused_events and paused_events[0]["payload"]["reason"] == "retry_ceiling"


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

    wf = tmp_path / "imp" / "workflows" / "imp"
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
        "      - {name: w, file: agents/writer.md, tier: medium}\n"
    )
    _write_fake_config(tmp_path)
    change = _make_change(tmp_path)

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


# ── agent-rule goto feeds back to the resumed target ────────────────────────


@pytest.mark.anyio
async def test_goto_feedback_resumes_target_with_wrapper(tmp_path: Path) -> None:
    """A reviewer agent that resumes backward with a feedback: preamble string
    makes the coder's NEXT invocation carry the composed feedback text.
    The reviewer flips to approve on its 2nd pass so the run completes."""
    wf = tmp_path / "imp" / "workflows" / "imp"
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
        "      - {name: coder, file: agents/coder.md, tier: medium}\n"
        "      - name: reviewer\n"
        "        file: agents/reviewer.md\n"
        "        tier: medium\n"
        "        rules:\n"
        "          - {if: {verdict: Reject}, resume: coder, feedback: \"reviewer findings:\"}\n"
        "          - {else: true, continue: true}\n"
    )
    _write_fake_config(tmp_path)
    change = _make_change(tmp_path)

    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    runs: list[tuple[str, str | None]] = []   # (agent_id, retry_feedback)
    reviewer_calls = {"n": 0}

    class _Runner:
        async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
            _persist_initial(invocation)
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
    assert coder_runs[1] is not None and "reviewer findings:" in coder_runs[1]
    assert "verdict: " in coder_runs[1]   # render_output_fields half is present

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
        "verb": "goto", "target": "coder", "resume": True, "rework": False}
    assert _action_payload(Goto("coder")) == {
        "verb": "goto", "target": "coder", "resume": False, "rework": False}
    # feedback present -> rework True (a loud reviewer rework on a plain goto edge)
    assert _action_payload(Goto("coder", feedback="Here's feedback:")) == {
        "verb": "goto", "target": "coder", "resume": False, "rework": True}
    # non-goto actions are unchanged
    assert _action_payload(Continue()) == {"verb": "continue"}


@pytest.mark.anyio
async def test_run_change_computes_and_persists_resolved_tiers(tmp_path):
    """After a run, State carries resolved_tiers and exactly one tiers_resolved
    event was emitted."""
    change, _, _ = _seed_minimal_workflow(tmp_path)
    _register_fake({"status": "Success"})

    captured: list[dict] = []
    await run_change(change, on_event=captured.append)

    state = State.load(change.state_dir)
    assert state.resolved_tiers is not None
    assert "medium" in state.resolved_tiers
    resolved_events = [e for e in captured if e["type"] == "tiers_resolved"]
    assert len(resolved_events) == 1
    assert "tiers" in resolved_events[0]["payload"]
    assert "steps" in resolved_events[0]["payload"]


@pytest.mark.anyio
async def test_resume_reuses_resolved_tiers_no_second_event(tmp_path):
    """A second run_change re-entry reuses the persisted map and emits NO second
    tiers_resolved (compute-if-absent guard)."""
    change, _, _ = _seed_minimal_workflow(tmp_path)
    _register_fake({"status": "Success"})
    await run_change(change)

    # Re-run run_change on the same (now-completed) change: it re-loads state,
    # sees resolved_tiers already set, and must not emit a second event.
    captured: list[dict] = []
    await run_change(change, on_event=captured.append)
    assert [e for e in captured if e["type"] == "tiers_resolved"] == []


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_fresh_start_seeds_initial_messages_with_imp_begin_tag(
    tmp_path: Path,
) -> None:
    """On a fresh agent step (no resume marker, no wrapper_text), the executor
    seeds the runner with exactly one user message whose sole TextBlock is the
    literal `<imp:begin/>` engine tag (replacing the legacy `"begin"` opener)."""
    from imp.providers import resolve

    change, _, _ = _seed_minimal_workflow(tmp_path)
    captured: dict[str, AgentInvocation] = {}

    class _CapRunner:
        async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
            captured["inv"] = invocation
            yield {"type": "agent_output", "payload": {"status": "Success"}}

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
        "fake", env_check=lambda: True, factory=lambda model: _CapProvider(model))

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED

    inv = captured["inv"]
    assert len(inv.initial_messages) == 1, inv.initial_messages
    msg = inv.initial_messages[0]
    assert msg.role == "user"
    assert len(msg.content) == 1
    assert msg.content[0].text.startswith("<imp:begin/>")


def _seed_back_edge_workflow(
    root: Path, verb: str, feedback: str | None = None
) -> Change:
    """coder(agent) -> reviewer(agent). reviewer rules: on rework, <verb>: coder;
    else continue. `verb` is 'goto' or 'resume'. `feedback` is an optional
    preamble string for the back edge (§6); None means bare (forwards nothing)."""
    wf = root / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "coder.md").write_text(
        "---\nname: coder\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n")
    (wf / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "  verdict: {type: enum, values: [approve, rework]}\n---\ngo\n")
    feedback_fragment = f', feedback: "{feedback}"' if feedback is not None else ""
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n"
        "  - name: impl\n"
        "    steps:\n"
        "      - {name: coder, file: agents/coder.md, tier: medium}\n"
        "      - name: reviewer\n"
        "        file: agents/reviewer.md\n"
        "        tier: medium\n"
        "        rules:\n"
        f"          - {{if: {{verdict: rework}}, {verb}: coder{feedback_fragment}}}\n"
        "          - {else: true, continue: true}\n")
    _write_fake_config(root)
    return _make_change(root)


def _register_capturing(coder_behavior, reviewer_verdicts: list[str], runs: list) -> None:
    """Register a fake provider. `runs` collects
    (agent_id, balance_on_resume, retry_feedback) per invocation.
    `coder_behavior(n)` returns one of: ("output", payload) | ("incomplete", None).
    `reviewer_verdicts` is consumed one per reviewer invocation."""
    from imp.providers import resolve
    resolve.reset_providers_for_tests()
    counts = {"coder": 0, "reviewer": 0}

    class _Runner:
        async def run(self, invocation: AgentInvocation):
            _persist_initial(invocation)
            runs.append((invocation.agent_id, invocation.balance_on_resume,
                         invocation.retry_feedback))
            name = invocation.agent_def.name
            if name == "coder":
                counts["coder"] += 1
                kind, payload = coder_behavior(counts["coder"])
                if kind == "incomplete":
                    yield {"type": "error", "payload": {
                        "category": "agent_incomplete",
                        "kind": "no_complete_step_after_nudges", "message": "x"}}
                    return
                yield {"type": "agent_output", "payload": payload}
            else:
                counts["reviewer"] += 1
                verdict = reviewer_verdicts[counts["reviewer"] - 1]
                yield {"type": "agent_output",
                       "payload": {"status": "Success", "verdict": verdict}}

        async def interrupt(self) -> None:
            pass

    class _Provider:
        name = "fake"
        model = "m"

        def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
            return _Runner()

    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda _m: _Provider())


@pytest.mark.anyio
async def test_resume_edge_keeps_transcript(tmp_path: Path) -> None:
    change = _seed_back_edge_workflow(tmp_path, verb="resume")
    runs: list = []
    _register_capturing(lambda _n: ("output", {"status": "Success"}),
                        ["rework", "approve"], runs)
    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED
    coder_balances = [bal for (aid, bal, _fb) in runs if aid.endswith("/coder")]
    # first entry fresh, second entry (via resume:) keeps the transcript
    assert coder_balances == [False, True]


@pytest.mark.anyio
async def test_goto_edge_starts_target_fresh(tmp_path: Path) -> None:
    change = _seed_back_edge_workflow(tmp_path, verb="goto")
    runs: list = []
    _register_capturing(lambda _n: ("output", {"status": "Success"}),
                        ["rework", "approve"], runs)
    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED
    coder_balances = [bal for (aid, bal, _fb) in runs if aid.endswith("/coder")]
    # both entries fresh: a plain goto never resumes
    assert coder_balances == [False, False]


@pytest.mark.anyio
async def test_resume_marker_is_one_shot(tmp_path: Path) -> None:
    change = _seed_back_edge_workflow(tmp_path, verb="resume")
    runs: list = []
    _register_capturing(lambda _n: ("output", {"status": "Success"}),
                        ["rework", "approve"], runs)
    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED
    # The reviewer that runs AFTER the resumed coder is a fresh entry (no marker).
    reviewer_balances = [bal for (aid, bal, _fb) in runs if aid.endswith("/reviewer")]
    assert reviewer_balances == [False, False]
    # Marker is cleared by completion.
    assert State.load(change.state_dir).resume_pending is None


@pytest.mark.anyio
async def test_resume_forwards_feedback_to_target(tmp_path: Path) -> None:
    """F2: a resume: edge with a feedback: preamble string delivers the composed
    feedback text to the resumed agent's next invocation (§6)."""
    change = _seed_back_edge_workflow(
        tmp_path, verb="resume", feedback="reviewer findings:")
    runs: list = []
    _register_capturing(lambda _n: ("output", {"status": "Success"}),
                        ["rework", "approve"], runs)
    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED
    coder_feedback = [fb for (aid, _bal, fb) in runs if aid.endswith("/coder")]
    assert coder_feedback[0] is None                  # first entry: no feedback
    assert coder_feedback[1] is not None              # resumed entry carries it
    assert "reviewer findings:" in coder_feedback[1]


@pytest.mark.anyio
async def test_resume_is_constant_across_error_retry(tmp_path: Path) -> None:
    """F1: a resumed coder that errors once then succeeds keeps balance_on_resume
    True on both attempts of the resumed entry (the threaded is_resume param is
    constant across the error recursion; the marker is never read in _route_agent)."""
    from imp.providers import resolve
    resolve.reset_providers_for_tests()
    change = _seed_back_edge_workflow(tmp_path, verb="resume")
    runs: list = []
    counts = {"coder": 0, "reviewer": 0}

    class _Runner:
        async def run(self, invocation: AgentInvocation):
            _persist_initial(invocation)
            runs.append((invocation.agent_id, invocation.balance_on_resume))
            if invocation.agent_def.name == "coder":
                counts["coder"] += 1
                # entry 1 (fresh) ok; entry 2 (resumed) errors once (missing
                # output -> RetryNow), entry 3 (retry of resumed) ok.
                if counts["coder"] == 2:
                    yield {"type": "error", "payload": {
                        "category": "agent_output_missing",
                        "kind": "missing", "message": "x"}}
                    return
                yield {"type": "agent_output", "payload": {"status": "Success"}}
            else:
                counts["reviewer"] += 1
                verdict = "rework" if counts["reviewer"] == 1 else "approve"
                yield {"type": "agent_output",
                       "payload": {"status": "Success", "verdict": verdict}}

        async def interrupt(self) -> None:
            pass

    class _Provider:
        name = "fake"
        model = "m"

        def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
            return _Runner()

    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda _m: _Provider())

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED
    coder_balances = [bal for (aid, bal) in runs if aid.endswith("/coder")]
    # entry1 fresh; entry2 + its retry (entry3) both resumed -> all keep True
    assert coder_balances == [False, True, True]


@pytest.mark.anyio
async def test_resume_survives_pause_and_reload(tmp_path: Path) -> None:
    """S1: a step entered via resume: that pauses mid-run keeps resume_pending in
    state.json, so the next run_change re-entry continues (does not wipe)."""
    from imp.providers import resolve
    resolve.reset_providers_for_tests()
    change = _seed_back_edge_workflow(tmp_path, verb="resume")
    runs: list = []
    counts = {"coder": 0, "reviewer": 0}

    class _Runner:
        async def run(self, invocation: AgentInvocation):
            _persist_initial(invocation)
            runs.append((invocation.agent_id, invocation.balance_on_resume))
            if invocation.agent_def.name == "coder":
                counts["coder"] += 1
                # entry 1 (fresh) ok; entry 2 (resumed) pauses fatally; entry 3
                # (resumed re-entry after reload) ok.
                if counts["coder"] == 2:
                    yield {"type": "error", "payload": {
                        "category": "agent_incomplete",
                        "kind": "no_complete_step_after_nudges", "message": "x"}}
                    return
                yield {"type": "agent_output", "payload": {"status": "Success"}}
            else:
                counts["reviewer"] += 1
                verdict = "rework" if counts["reviewer"] == 1 else "approve"
                yield {"type": "agent_output",
                       "payload": {"status": "Success", "verdict": verdict}}

        async def interrupt(self) -> None:
            pass

    class _Provider:
        name = "fake"
        model = "m"

        def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
            return _Runner()

    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda _m: _Provider())

    # First run: coder#1 ok -> reviewer rework -> resume:coder -> coder#2 pauses.
    first = await run_change(change, on_event=lambda _e: None)
    assert first is RunResult.PAUSED
    paused = State.load(change.state_dir)
    assert paused.step == "coder"
    assert paused.resume_pending == "coder"      # marker survived the pause

    # Second run: reloads, coder is still the resume target -> continues.
    second = await run_change(change, on_event=lambda _e: None)
    assert second is RunResult.COMPLETED
    coder_balances = [bal for (aid, bal) in runs if aid.endswith("/coder")]
    # entry1 fresh; entry2 (paused) resumed; entry3 (post-reload) resumed
    assert coder_balances == [False, True, True]


@pytest.mark.anyio
async def test_resume_keep_branch_passes_empty_initial_messages(
    tmp_path: Path,
) -> None:
    """On the resume keep-branch the executor must not construct any opener
    message: providers replay the on-disk transcript and never iterate
    initial_messages here. The first (fresh) coder entry still carries the
    `<imp:begin/>` seed; the second (resumed) entry carries `()`."""
    from imp.providers import resolve

    change = _seed_back_edge_workflow(tmp_path, verb="resume")
    invs: list[AgentInvocation] = []
    reviewer_calls = {"n": 0}

    class _Runner:
        async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
            # Snapshot initial_messages BEFORE persisting (the assertions inspect
            # the per-entry seed; persistence only affects the on-disk transcript).
            invs.append(invocation)
            _persist_initial(invocation)
            if invocation.agent_def.name == "reviewer":
                reviewer_calls["n"] += 1
                verdict = "rework" if reviewer_calls["n"] == 1 else "approve"
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

    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda _m: _Provider())

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED

    # Defensive: assert overall shape (coder, reviewer, coder, reviewer) before
    # slicing — protects against a future workflow change re-ordering the
    # back-edge dispatch and silently masking the resume-branch assertion.
    assert [i.agent_def.name for i in invs] == ["coder", "reviewer", "coder", "reviewer"]
    coder_invs = [i for i in invs if i.agent_def.name == "coder"]
    assert len(coder_invs) == 2, [i.agent_def.name for i in invs]

    # Fresh entry: one seed message containing <imp:begin/> plus the Change name.
    assert len(coder_invs[0].initial_messages) == 1
    assert coder_invs[0].initial_messages[0].content[0].text.startswith("<imp:begin/>")

    # Resume-kept entry: NO seed message at all.
    assert coder_invs[1].initial_messages == ()


# ── script-step pause emits script_blocked event ────────────────────────────


@pytest.mark.anyio
async def test_script_pause_emits_script_blocked_event(tmp_path: Path) -> None:
    """A script step that exits non-zero and matches a {else: true, pause: true}
    rule must journal a script_blocked event carrying the step name, exit code,
    and stderr text.  Previously the pause was bare (no reason) and all stderr
    was silently discarded."""
    wf = tmp_path / "imp" / "workflows" / "imp"
    wf.mkdir(parents=True)
    # A single script step: exit 7 with stderr text; the else-rule pauses.
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - name: check\n"
        "        command: 'echo boom-remediation >&2; exit 7'\n"
        "        rules:\n"
        "          - {else: true, pause: true}\n"
    )
    _write_fake_config(tmp_path)
    # The sandbox policy binds control_root/.git; git init ensures bwrap finds it.
    import subprocess
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    change = _make_change(tmp_path)
    _register_fake({"status": "Success"})  # resolve_run needs a registered provider

    result = await run_change(change, on_event=lambda _e: None)

    assert result is RunResult.PAUSED
    state = State.load(change.state_dir)
    assert state.status == "paused"

    evs = [e for e in change.event_log().read() if e.get("type") == "script_blocked"]
    assert evs, "expected at least one script_blocked event"
    last = evs[-1]
    assert last["payload"]["step"] == "check"
    assert last["payload"]["exit"] == 7
    assert "boom-remediation" in last["payload"]["stderr"]


# ── _last_script_blocked selection logic ────────────────────────────────────


def test_last_script_blocked_returns_last_match() -> None:
    """_last_script_blocked returns the LAST event whose payload.step matches,
    ignoring events for other steps and returning None when absent."""
    from imp.cli.main import _last_script_blocked

    events: list[dict] = [
        {"type": "script_blocked", "payload": {"step": "check", "stderr": "first", "exit": 3}},
        {"type": "script_blocked", "payload": {"step": "other", "stderr": "noise", "exit": 1}},
        {"type": "script_blocked", "payload": {"step": "check", "stderr": "second", "exit": 5}},
    ]

    # Returns the LAST match for "check" (index 2), not the first (index 0).
    result = _last_script_blocked(events, "check")
    assert result is not None
    assert result["payload"]["stderr"] == "second"

    # Returns the sole match for "other".
    result_other = _last_script_blocked(events, "other")
    assert result_other is not None
    assert result_other["payload"]["stderr"] == "noise"

    # Returns None when the step name is absent.
    assert _last_script_blocked(events, "missing") is None

    # Returns None for an empty event list.
    assert _last_script_blocked([], "check") is None

    # Ignores non-script_blocked events.
    mixed: list[dict] = [
        {"type": "step_started", "payload": {"step": "check"}},
        {"type": "script_blocked", "payload": {"step": "check", "stderr": "only", "exit": 4}},
        {"type": "step_completed", "payload": {"step": "check"}},
    ]
    assert _last_script_blocked(mixed, "check") == mixed[1]


# ── Workflow snapshot ────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_first_run_snapshots_the_workflow(tmp_path: Path) -> None:
    """The first run copies the live workflow dir into the gitignored per-Change
    snapshot at .imp/change/workflow/ (taken once, at run-start)."""
    change, _, _ = _seed_minimal_workflow(tmp_path)
    _register_fake({"status": "Success"})
    snapshot = change.change_dir / "workflow"
    assert not snapshot.exists()

    result = await run_change(change, on_event=lambda _e: None)

    assert result is RunResult.COMPLETED
    assert (snapshot / "workflow.yaml").exists()
    assert (snapshot / "agents" / "writer.md").exists()


@pytest.mark.anyio
async def test_snapshot_copy_cleans_stale_tmp_dir(tmp_path: Path) -> None:
    """A leftover workflow.tmp from a previous crashed copy is removed before the
    new copy proceeds, so the final workflow/ snapshot is always complete."""
    change, _, _ = _seed_minimal_workflow(tmp_path)
    _register_fake({"status": "Success"})
    # Simulate a partial copy left over from a prior crash.
    tmp = change.change_dir / "workflow.tmp"
    tmp.mkdir()
    (tmp / "stale.yaml").write_text("garbage")

    result = await run_change(change, on_event=lambda _e: None)

    assert result is RunResult.COMPLETED
    # The stale tmp dir should be gone.
    assert not tmp.exists()
    # And the real snapshot should be present and correct.
    snapshot = change.change_dir / "workflow"
    assert (snapshot / "workflow.yaml").exists()


@pytest.mark.anyio
async def test_resume_runs_frozen_snapshot_after_live_workflow_deleted(
    tmp_path: Path,
) -> None:
    """A resume reuses the existing snapshot and never re-copies: even after the
    entire live workflow dir (script + step) is deleted mid-run, the resume still
    runs the frozen snapshot version."""
    import shutil
    import subprocess

    wf = tmp_path / "imp" / "workflows" / "imp"
    (wf / "scripts").mkdir(parents=True)
    (wf / "scripts" / "probe.sh").write_text("echo frozen >&2\nexit 7\n")
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - name: check\n"
        "        command: sh scripts/probe.sh\n"
        "        rules:\n"
        "          - {else: true, pause: true}\n"
    )
    _write_fake_config(tmp_path)
    # The sandbox policy binds control_root/.git; git init ensures bwrap finds it.
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    change = _make_change(tmp_path)
    _register_fake({"status": "Success"})  # resolve_run needs a registered provider

    r1 = await run_change(change, on_event=lambda _e: None)
    assert r1 is RunResult.PAUSED
    snapshot = change.change_dir / "workflow"
    assert (snapshot / "scripts" / "probe.sh").exists()

    # Mutate the live workflow out from under the run: delete script AND step.
    shutil.rmtree(wf)

    # Resume reuses the frozen snapshot — no re-copy from the now-missing live
    # dir (which would raise), and the deleted script still runs.
    r2 = await run_change(change, on_event=lambda _e: None)
    assert r2 is RunResult.PAUSED
    evs = [e for e in change.event_log().read()
           if e.get("type") == "script_blocked"]
    assert evs and evs[-1]["payload"]["step"] == "check"
    # exit 7 proves the snapshot's probe.sh actually executed on resume.
    assert evs[-1]["payload"]["exit"] == 7


# ── Cursor vs absent step guard ──────────────────────────────────────────────


@pytest.mark.anyio
async def test_none_step_resolves_to_complete(tmp_path: Path) -> None:
    """state.step is None is genuine completion: the run finalizes COMPLETED."""
    change, _, _ = _seed_minimal_workflow(tmp_path)
    _register_fake({"status": "Success"})
    state = State.load(change.state_dir)
    state.step = None
    state.save(change.state_dir)

    result = await run_change(change, on_event=lambda _e: None)

    assert result is RunResult.COMPLETED
    assert State.load(change.state_dir).status == "completed"


@pytest.mark.anyio
async def test_unknown_step_pauses_without_silent_merge(tmp_path: Path) -> None:
    """A cursor naming a step absent from the loaded workflow pauses with an
    unknown_step error rather than calling _complete (which could silent-merge)."""
    change, _, _ = _seed_minimal_workflow(tmp_path)
    _register_fake({"status": "Success"})
    state = State.load(change.state_dir)
    state.step = "ghost-step"
    state.save(change.state_dir)

    result = await run_change(change, on_event=lambda _e: None)

    assert result is RunResult.PAUSED
    reloaded = State.load(change.state_dir)
    assert reloaded.status == "paused"
    assert reloaded.step == "ghost-step"          # cursor untouched, not nulled
    events = list(change.event_log().read())  # read() is a single-use generator
    paused = [e for e in events if e.get("type") == "workflow_paused"]
    assert paused and paused[-1]["payload"]["reason"] == "unknown_step"
    assert paused[-1]["payload"]["step"] == "ghost-step"
    assert not any(e.get("type") == "change_merged" for e in events)


@pytest.mark.anyio
async def test_frozen_script_runs_from_snapshot_cwd_with_live_project_root(
    tmp_path: Path,
) -> None:
    """A script gate runs with cwd under .imp/change/workflow/ (the snapshot,
    sandbox binding intact) while IMP_PROJECT_ROOT still points at the worktree, so
    git ops act on the change's real tree. Exit 0 -> continue -> COMPLETED."""
    import subprocess

    wf = tmp_path / "imp" / "workflows" / "imp"
    wf.mkdir(parents=True)
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - name: probe\n"
        "        command: 'case \"$(pwd)\" in */.imp/change/workflow) ;; *) exit 9;; esac;"
        " cd \"$IMP_PROJECT_ROOT\"; git rev-parse --is-inside-work-tree'\n"
        "        rules:\n"
        "          - {if: {exit: 0}, continue: true}\n"
        "          - {else: true, pause: true}\n"
    )
    _write_fake_config(tmp_path)
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    change = _make_change(tmp_path)
    _register_fake({"status": "Success"})

    result = await run_change(change, on_event=lambda _e: None)

    assert result is RunResult.COMPLETED


@pytest.mark.anyio
async def test_facts_render_into_main_agent_prompt(tmp_path: Path) -> None:
    wf = tmp_path / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\n"
        "run {{ project.test_command }}\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n  - name: only\n    steps:\n"
        "      - {name: w, file: agents/writer.md, tier: medium}\n"
    )
    (tmp_path / ".imp").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".imp" / "config.yaml").write_text(_FAKE_CONFIG)
    (tmp_path / "imp" / "project.yaml").write_text(
        "default-workflow: imp\nfacts:\n  test_command: make check\n"
    )
    change = _make_change(tmp_path)
    sink: list[str] = []
    _register_prompt_capturing({"status": "Success"}, sink)

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED
    assert any("run make check" in p for p in sink)


def test_change_context_defaults_empty_facts() -> None:
    from imp.agents.runner import ChangeContext
    cc = ChangeContext(
        handle="s", title="t", project_root=Path("/x"), workflow_dir=Path("/x"))
    assert cc.project_facts == {}
    assert cc.guidance == ""

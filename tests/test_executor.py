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
from imp.engine.feedback import COMPLETION_BACKSTOP
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


def _make_change(root: Path, *, change_id: str = "test-change", title: str = "t") -> Change:
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
        "id": change_id, "title": title, "base_branch": "main",
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
        id=change_id,
        step=flat[0].name if flat else None,
        step_attempt=0,
        status="running",
        vars=dict(workflow.vars.defaults()),
        loop_counts={},
    ).save(change_dir / "state")

    change = Change(
        id=change_id, title=title, base_branch="main",
        control_root=root, project_root=root, change_dir=change_dir,
    )
    change.event_log().append(
        agent_id=None, type="change_created",
        payload={"id": change_id, "title": title},
    )
    return change


class _FakeRunner:
    def __init__(self, output: dict) -> None:
        self._output = output

    async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
        yield {"type": "agent_output", "payload": {"outcome": "ok", **self._output}}

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
        yield {"type": "agent_output", "payload": {"outcome": "ok", **self._output}}

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


class _InvocationCapturingRunner:
    def __init__(self, output: dict, sink: list) -> None:
        self._output = output
        self._sink = sink

    async def run(self, invocation: AgentInvocation) -> AsyncGenerator[Event, None]:
        self._sink.append(invocation)
        yield {"type": "agent_output", "payload": {"outcome": "ok", **self._output}}

    async def interrupt(self) -> None:
        pass


class _InvocationCapturingProvider:
    name = "fake"

    def __init__(self, output: dict, sink: list, model: str = "m") -> None:
        self._output = output
        self._sink = sink
        self.model = model

    def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
        return _InvocationCapturingRunner(self._output, self._sink)


def _register_invocation_capturing(output: dict, sink: list) -> None:
    from imp.providers import resolve
    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "fake", env_check=lambda: True,
        factory=lambda model: _InvocationCapturingProvider(output, sink, model=model),
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
        "go {{ change.id }}\n"
    )
    (wf_dir / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n"
        f"  - name: {phase_name}\n"
        "    steps:\n"
        f"      - {{name: {step_name}, file: agents/{agent_stem}.md, tier: low}}\n"
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
        "        tier: low\n"
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
        "        tier: low\n"
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


def _seed_lane_release_workflow(root: Path) -> Change:
    wf = root / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True, exist_ok=True)
    (wf / "agents" / "worker.md").write_text(
        "---\nname: worker\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "  deviation: {type: bool}\n---\ndo work\n"
    )
    (wf / "agents" / "done.md").write_text(
        "---\nname: done\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\nfinish\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n"
        "  - name: work\n"
        "    steps:\n"
        "      - name: wait-for-head\n"
        "        acquire: merge-lane\n"
        "        rules:\n"
        "          - {else: true, continue: true}\n"
        "      - name: worker\n"
        "        file: agents/worker.md\n"
        "        tier: low\n"
        "        rules:\n"
        "          - {if: {deviation: true}, release: merge-lane, goto: done}\n"
        "          - {else: true, continue: true}\n"
        "  - name: finish\n"
        "    steps:\n"
        "      - name: done\n"
        "        file: agents/done.md\n"
        "        tier: low\n"
    )
    _write_fake_config(root)
    return _make_change(root)


@pytest.mark.anyio
async def test_goto_with_release_releases_held_lane(tmp_path: Path) -> None:
    """Goto(release="merge-lane") in a rule releases the held lane via the executor branch."""
    change = _seed_lane_release_workflow(tmp_path)
    events: list = []
    _register_fake({"status": "Success", "deviation": True})

    result = await run_change(change, on_event=events.append)

    assert result is RunResult.COMPLETED
    state = State.load(change.state_dir)
    assert state.held_lane is None
    released = [e for e in events if e.get("type") == "lane_released"]
    assert len(released) == 1
    assert released[0]["payload"]["step"] == "worker"


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
        "      - {name: writer, file: agents/writer.md, tier: low}\n"
        "      - name: reviewer\n"
        "        file: agents/reviewer.md\n"
        "        tier: low\n"
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
                       "payload": {"outcome": "ok", "status": "Success", "verdict": "rework"}}
            else:
                yield {"type": "agent_output", "payload": {"outcome": "ok", "status": "Success"}}

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
        "        tier: low\n"
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
    def __init__(self, *, tool: str = "sub_agent", message: str = "boom") -> None:
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
        tool="sub_agent",
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
    assert fatal_events[0]["payload"]["tool"] == "sub_agent"


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
async def test_fatal_error_pauses_without_burning_retries(tmp_path: Path) -> None:
    change, _, _ = _seed_minimal_workflow(tmp_path)

    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    runner = _CategoryErrorRunner(
        category="terminated", kind="TerminatedError",
        message="agent terminated with an error (cause unknown)",
    )

    class _FatalProvider:
        name = "fake"
        model = "m"

        def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
            return runner

    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda _m: _FatalProvider(),
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
        yield {"type": "agent_output", "payload": {"outcome": "ok", **self._output}}

    async def interrupt(self) -> None:
        pass


class _AuthThenSuccessRunner:
    """Yields an auth error for the first 7 runs, then succeeds. Cumulative
    backoff before the 8th run is 1+2+4+8+16+32+60 = 123 s — past the old 120 s
    auth budget, under the new 1800 s one."""
    def __init__(self, output: dict) -> None:
        self._output = output
        self.run_count = 0

    async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
        self.run_count += 1
        if self.run_count <= 7:
            yield {"type": "error", "payload": {
                "category": "auth",
                "kind": "http_401",
                "message": "401 Invalid authentication credentials",
            }}
            return
        yield {"type": "agent_output", "payload": {"outcome": "ok", **self._output}}

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
async def test_retry_budget_pauses_with_retry_ceiling(
    tmp_path: Path, monkeypatch,
) -> None:
    """Per-category budget: a transient error that can't recover within the
    budget pauses cleanly with reason="retry_ceiling" — in interactive runs too
    (the deliberate mode-independent behavior change)."""
    from imp.engine import executor
    # Tiny budget so the first retry delay (1.0s) exceeds it on attempt 1.
    monkeypatch.setattr(executor, "budget_for", lambda category: 0.5)
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
    result = await run_change(change, on_event=events.append)
    assert result is RunResult.PAUSED
    # Budget check fires BEFORE the first retry sleep -> exactly one run.
    assert runner.run_count == 1
    paused_events = [e for e in events if e["type"] == "workflow_paused"]
    assert paused_events and paused_events[0]["payload"]["reason"] == "retry_ceiling"


@pytest.mark.anyio
async def test_auth_budget_exhaustion_pauses_with_reauth_needed(
    tmp_path: Path, monkeypatch,
) -> None:
    """AC-B: when the auth budget is exceeded, the executor pauses with
    reason="reauth_needed" (kind passes through from the labeled error, here
    http_401). Non-auth categories keep retry_ceiling (covered by
    test_retry_budget_pauses_with_retry_ceiling)."""
    from imp.engine import executor
    # Tiny budget so the first retry delay (1.0s) exceeds it on attempt 1.
    monkeypatch.setattr(executor, "budget_for", lambda category: 0.5)
    async def _instant_sleep(delay, coordinator, *, tick=0.1):  # type: ignore[misc]
        pass
    monkeypatch.setattr(executor, "_interruptible_sleep", _instant_sleep)

    change, _, _ = _seed_minimal_workflow(tmp_path)
    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    runner = _CategoryErrorRunner(
        category="auth", kind="http_401",
        message="401 Invalid authentication credentials",
    )

    class _AuthProvider:
        name = "fake"
        model = "m"

        def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
            return runner

    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda _m: _AuthProvider(),
    )

    events: list[dict] = []
    result = await run_change(change, on_event=events.append)
    assert result is RunResult.PAUSED
    assert runner.run_count == 1
    paused = [e for e in events if e["type"] == "workflow_paused"]
    assert paused and paused[0]["payload"]["reason"] == "reauth_needed"


@pytest.mark.anyio
async def test_auth_backoff_crosses_old_120s_budget_and_keeps_retrying(
    tmp_path: Path, monkeypatch,
) -> None:
    """AC-A: with the real 1800 s auth budget, an auth-error backoff whose
    cumulative delay crosses 120 s (123 s after 7 retries) keeps retrying and
    completes. Under the old 120 s budget this run paused at attempt 7."""
    from imp.engine import executor
    async def _instant_sleep(delay, coordinator, *, tick=0.1):  # type: ignore[misc]
        pass
    monkeypatch.setattr(executor, "_interruptible_sleep", _instant_sleep)

    change, _, _ = _seed_minimal_workflow(tmp_path)
    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    runner = _AuthThenSuccessRunner({"status": "Success"})

    class _AuthProvider:
        name = "fake"
        model = "m"

        def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
            return runner

    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda _m: _AuthProvider(),
    )

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED
    # 7 auth errors then success; the run rode through the 123 s crossing.
    assert runner.run_count == 8


@pytest.mark.anyio
async def test_fatal_api_error_pauses(tmp_path: Path) -> None:
    """A runner that emits a fatal (Pause) error makes the step Pause, not retry."""
    change, _, _ = _seed_minimal_workflow(tmp_path)

    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    class _IncompleteRunner:
        async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
            yield {"type": "error", "payload": {
                "category": "api",
                "kind": "api_error",
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
        "      - {name: w, file: agents/writer.md, tier: low}\n"
    )
    _write_fake_config(tmp_path)
    change = _make_change(tmp_path)

    captured: dict[str, AgentInvocation] = {}

    class _CapRunner:
        async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
            captured["inv"] = invocation
            yield {"type": "agent_output",
                   "payload": {"outcome": "ok", "status": "Success", "verdict": "approve"}}

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
    task_msg = inv.initial_messages[0].content[0].text
    assert "complete_step" in task_msg
    for token in ("status", "Success", "Error", "verdict", "approve", "rework"):
        assert token in task_msg, token
    assert inv.continuation_fn is not None
    from imp.engine.awaiting_user_input import AwaitingUserInput
    from imp.engine.feedback import COMPLETION_NUDGE
    # Headless top-level schema-bearing step: the FIRST bare turn-end is nudged
    # once (asked to complete or confirm WAITING); a subsequent bare turn-end
    # then parks via AwaitingUserInput carrying the original text.
    assert inv.continuation_fn("prose") == COMPLETION_NUDGE
    with pytest.raises(AwaitingUserInput):
        inv.continuation_fn("prose")


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
        "      - {name: coder, file: agents/coder.md, tier: low}\n"
        "      - name: reviewer\n"
        "        file: agents/reviewer.md\n"
        "        tier: low\n"
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
                       "payload": {"outcome": "ok", "status": "Success", "verdict": verdict}}
            else:
                yield {"type": "agent_output", "payload": {"outcome": "ok", "status": "Success"}}

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
    # The run-start event carries the configured checkpoint threshold so the
    # renderer/engine can resolve a warn/checkpoint cutoff. Default config -> 0.8.
    assert resolved_events[0]["payload"]["checkpoint_threshold"] == 0.8


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
async def test_fresh_start_puts_task_in_first_user_message(
    tmp_path: Path,
) -> None:
    """On a fresh agent step the executor delivers the task (rendered template +
    completion contract) as the sole first user message — no `<imp:begin/>` sentinel."""
    from imp.providers import resolve

    change, _, _ = _seed_minimal_workflow(tmp_path)
    captured: dict[str, AgentInvocation] = {}

    class _CapRunner:
        async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
            captured["inv"] = invocation
            yield {"type": "agent_output", "payload": {"outcome": "ok", "status": "Success"}}

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
    text = msg.content[0].text
    assert "<imp:begin/>" not in text                 # sentinel gone
    assert "complete_step" in text                    # relocated completion contract


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
        "      - {name: coder, file: agents/coder.md, tier: low}\n"
        "      - name: reviewer\n"
        "        file: agents/reviewer.md\n"
        "        tier: low\n"
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
                        "category": "api",
                        "kind": "api_error", "message": "x"}}
                    return
                yield {"type": "agent_output", "payload": {"outcome": "ok", **payload}}
            else:
                counts["reviewer"] += 1
                verdict = reviewer_verdicts[counts["reviewer"] - 1]
                yield {"type": "agent_output",
                       "payload": {"outcome": "ok", "status": "Success", "verdict": verdict}}

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
async def test_goto_feedback_prepended_to_fresh_user_message(tmp_path: Path) -> None:
    """A goto: edge with a feedback: preamble delivers that feedback as the lead of
    the fresh target's first user message (the relocated seed), preserving rework
    feedback for goto-fed agents whose templates do not render run.opening."""
    change = _seed_back_edge_workflow(
        tmp_path, verb="goto", feedback="Address these findings:")
    invs: list[AgentInvocation] = []

    class _Rec:
        async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
            invs.append(invocation)
            _persist_initial(invocation)
            if invocation.agent_def.name == "reviewer":
                reviewer_n = len([i for i in invs if i.agent_def.name == "reviewer"])
                verdict = "rework" if reviewer_n == 1 else "approve"
                yield {"type": "agent_output",
                       "payload": {"outcome": "ok", "verdict": verdict, "status": "Success"}}
            else:
                yield {"type": "agent_output",
                       "payload": {"outcome": "ok", "status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _Prov:
        name = "fake"

        def __init__(self, model: str = "m") -> None:
            self.model = model

        def create_runner(self, invocation: AgentInvocation) -> _Rec:
            return _Rec()

    from imp.providers import resolve
    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda model: _Prov(model))

    await run_change(change, on_event=lambda _e: None, headless=True)

    coder_invs = [i for i in invs if i.agent_def.name == "coder"]
    assert len(coder_invs) == 2
    reworked = coder_invs[1].initial_messages[0].content[0].text
    assert "Address these findings:" in reworked   # feedback_lead from goto edge
    assert "complete_step" in reworked


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
                yield {"type": "agent_output", "payload": {"outcome": "ok", "status": "Success"}}
            else:
                counts["reviewer"] += 1
                verdict = "rework" if counts["reviewer"] == 1 else "approve"
                yield {"type": "agent_output",
                       "payload": {"outcome": "ok", "status": "Success", "verdict": verdict}}

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
                        "category": "api",
                        "kind": "api_error", "message": "x"}}
                    return
                yield {"type": "agent_output", "payload": {"outcome": "ok", "status": "Success"}}
            else:
                counts["reviewer"] += 1
                verdict = "rework" if counts["reviewer"] == 1 else "approve"
                yield {"type": "agent_output",
                       "payload": {"outcome": "ok", "status": "Success", "verdict": verdict}}

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
    initial_messages here. The first (fresh) coder entry carries one user message
    (the relocated task, no `<imp:begin/>` sentinel); the second (resumed) entry
    carries `()`."""
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
                       "payload": {"outcome": "ok", "status": "Success", "verdict": verdict}}
            else:
                yield {"type": "agent_output", "payload": {"outcome": "ok", "status": "Success"}}

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

    # Fresh entry: one user message carrying the relocated task + completion
    # contract (no <imp:begin/> sentinel).
    assert len(coder_invs[0].initial_messages) == 1
    fresh = coder_invs[0].initial_messages[0].content[0].text
    assert "<imp:begin/>" not in fresh
    assert "complete_step" in fresh

    # Resume-kept entry: NO seed message at all (transcript replays).
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


@pytest.mark.anyio
async def test_script_pause_emits_workflow_paused_with_script_blocked_reason(
    tmp_path: Path,
) -> None:
    """A script gate that exits non-zero into a pause rule must emit
    workflow_paused with reason 'script_blocked' (not the bare-pause default
    'interrupt'), while leaving status 'paused'."""
    wf = tmp_path / "imp" / "workflows" / "imp"
    wf.mkdir(parents=True)
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - name: check\n"
        "        command: 'echo boom >&2; exit 7'\n"
        "        rules:\n"
        "          - {else: true, pause: true}\n"
    )
    _write_fake_config(tmp_path)
    import subprocess
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    change = _make_change(tmp_path)
    _register_fake({"status": "Success"})

    result = await run_change(change, on_event=lambda _e: None)

    assert result is RunResult.PAUSED
    assert State.load(change.state_dir).status == "paused"
    paused = [e for e in change.event_log().read()
              if e.get("type") == "workflow_paused"]
    assert paused, "expected a workflow_paused event"
    assert paused[-1]["payload"]["reason"] == "script_blocked"


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
        "      - {name: w, file: agents/writer.md, tier: low}\n"
    )
    (tmp_path / ".imp").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".imp" / "config.yaml").write_text(_FAKE_CONFIG)
    (tmp_path / "imp" / "project.yaml").write_text(
        "default-workflow: imp\nfacts:\n  test_command: make check\n"
    )
    change = _make_change(tmp_path)
    sink: list = []
    _register_invocation_capturing({"status": "Success"}, sink)

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED
    assert any("run make check" in i.initial_messages[0].content[0].text for i in sink)


def test_change_context_defaults_empty_facts() -> None:
    from imp.agents.runner import ChangeContext
    cc = ChangeContext(
        title="t", project_root=Path("/x"), workflow_dir=Path("/x"))
    assert cc.project_facts == {}
    assert cc.guidance == ""


@pytest.mark.anyio
async def test_tolerated_forward_advance_resets_gate_loop_counter(
    tmp_path: Path,
) -> None:
    """Forward-continue from the gate (not task-lead) resets the gate's own loop counter.

    A tolerated cycle costs 0 of the gate's max_loops budget.
    """
    import subprocess

    wf = tmp_path / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "lead.md").write_text(
        "---\nname: lead\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "  tol: {type: bool}\n---\ngo\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "vars:\n  carry: {type: bool, default: false}\n"
        "defaults: {max_retries: 1, max_loops: 10}\n"
        "phases:\n"
        "  - name: work\n"
        "    steps:\n"
        "      - name: lead\n"
        "        file: agents/lead.md\n"
        "        tier: low\n"
        "        rules:\n"
        "          - {if: {tol: true}, set: {carry: true}, goto: gate}\n"
        "          - {else: true, continue: true}\n"
        "      - name: gate\n"
        "        command: 'exit 1'\n"
        "        max_loops: 2\n"
        "        rules:\n"
        "          - {if: {exit: 0}, continue: true}\n"
        "          - {if: {carry: true}, set: {carry: false}, continue: true}\n"
        "          - {else: true, resume: lead}\n"
    )
    _write_fake_config(tmp_path)
    # git init: the sandbox policy binds control_root/.git.
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    change = _make_change(tmp_path)

    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    class _Runner:
        async def run(self, invocation: AgentInvocation):
            # Gate resumes have empty initial_messages; those tolerate.
            tol = not invocation.initial_messages
            _persist_initial(invocation)
            yield {"type": "agent_output",
                   "payload": {"outcome": "ok", "status": "Success", "tol": tol}}

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

    result = await run_change(change, on_event=lambda _e: None)

    # No max_loops pause: the tolerated cycle completed.
    assert result is RunResult.COMPLETED
    state = State.load(change.state_dir)
    # The forward advance fired FROM the gate, popping its counter: the tolerated
    # cycle cost 0 of the gate's budget. The rejected design leaves this == 1.
    assert state.loop_counts.get("gate", 0) == 0


# ── outcome_history capture ──────────────────────────────────────────────────


def test_record_outcome_appends_and_bounds_to_k() -> None:
    from imp.engine.executor import OUTCOME_HISTORY_K, _record_outcome

    state = State(id="c1", step="writer", step_attempt=0, status="running")
    for i in range(6):
        _record_outcome(state, "writer", {"outcome": f"lap{i}"}, head=f"sha{i}")
    hist = state.outcome_history["writer"]
    assert OUTCOME_HISTORY_K == 4
    assert len(hist) == 4
    # Oldest two evicted; laps stay monotonic across eviction.
    assert [e["lap"] for e in hist] == [3, 4, 5, 6]
    assert [e["outcome"] for e in hist] == ["lap2", "lap3", "lap4", "lap5"]
    # Each entry carries the worktree HEAD sha recorded at completion.
    assert [e["head"] for e in hist] == ["sha2", "sha3", "sha4", "sha5"]


@pytest.mark.anyio
async def test_run_change_records_step_outcome(tmp_path: Path) -> None:
    change, _, _ = _seed_minimal_workflow(tmp_path)
    _register_fake({"status": "Success", "outcome": "did the thing"})

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED

    state = State.load(change.state_dir)
    assert state.outcome_history["w"] == [
        {"lap": 1, "outcome": "did the thing", "head": ""}]
    # outcome is never promoted into the rule-evaluation var environment.
    assert "outcome" not in state.vars


@pytest.mark.anyio
async def test_agent_step_without_outcome_fails_to_complete(tmp_path: Path) -> None:
    # With the field required on the executor path, a complete_step payload that
    # omits outcome fails validation, exhausts the single retry, and pauses —
    # nothing is recorded.
    change, _, _ = _seed_minimal_workflow(tmp_path)
    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    class _NoOutcomeRunner:
        async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
            yield {"type": "agent_output", "payload": {"status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _Provider:
        name = "fake"
        model = "m"

        def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
            return _NoOutcomeRunner()

    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda _m: _Provider())

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.PAUSED

    state = State.load(change.state_dir)
    assert state.outcome_history == {}


def test_render_prior_attempts_format() -> None:
    from imp.engine.executor import _render_prior_attempts

    text = _render_prior_attempts("review", [
        {"lap": 2, "outcome": "plan still on the code branch — rework"},
        {"lap": 4, "outcome": "same finding: plan on code branch — rework"},
    ])
    expected = (
        '⟳ Prior attempts at this step:\n'
        '   lap 2:  review — "plan still on the code branch — rework"\n'
        '   lap 4:  review — "same finding: plan on code branch — rework"'
    )
    assert text == expected


@pytest.mark.anyio
async def test_rework_reentry_injects_prior_attempts(tmp_path: Path) -> None:
    wf = tmp_path / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n")
    (wf / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "  verdict: {type: enum, values: [approve, rework]}\n---\ngo\n")
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 10}\n"
        "phases:\n"
        "  - name: work\n"
        "    steps:\n"
        "      - {name: writer, file: agents/writer.md, tier: low}\n"
        "      - name: reviewer\n"
        "        file: agents/reviewer.md\n"
        "        tier: low\n"
        "        max_loops: 3\n"
        "        rules:\n"
        "          - {if: {verdict: rework}, goto: writer}\n"
        "          - {else: true, continue: true}\n")
    _write_fake_config(tmp_path)
    change = _make_change(tmp_path)

    writer_openings: list[str] = []
    review_count = [0]
    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    class _Runner:
        def __init__(self, name: str) -> None:
            self._name = name

        async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
            if self._name == "writer":
                writer_openings.append(
                    invocation.initial_messages[0].content[0].text)
                yield {"type": "agent_output", "payload": {
                    "status": "Success", "outcome": "WROTE THE PLAN"}}
            else:
                verdict = "rework" if review_count[0] == 0 else "approve"
                review_count[0] += 1
                yield {"type": "agent_output", "payload": {
                    "status": "Success", "verdict": verdict, "outcome": "reviewed"}}

        async def interrupt(self) -> None:
            pass

    class _Provider:
        name = "fake"
        model = "m"

        def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
            return _Runner(invocation.agent_def.name)

    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda _m: _Provider())

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED

    # Writer ran twice: the fresh entry, then the rework re-entry.
    assert len(writer_openings) == 2
    assert "Prior attempts at this step:" not in writer_openings[0]
    assert "Prior attempts at this step:" in writer_openings[1]
    assert "WROTE THE PLAN" in writer_openings[1]
    assert "lap 1" in writer_openings[1]


@pytest.mark.anyio
async def test_step_lap_render_var_increments_across_bounce(tmp_path: Path) -> None:
    # writer emits `lap={{ step.lap }}` in its rendered prompt body; reviewer votes
    # `rework` on the first lap only, bouncing back to writer. The writer's
    # rendered lap must read 1 on the first pass and 2 on the re-entry.
    wf = tmp_path / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\n"
        "lap={{ step.lap }}\n")
    (wf / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "  verdict: {type: enum, values: [approve, rework]}\n---\ngo\n")
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 10}\n"
        "phases:\n"
        "  - name: work\n"
        "    steps:\n"
        "      - {name: writer, file: agents/writer.md, tier: low}\n"
        "      - name: reviewer\n"
        "        file: agents/reviewer.md\n"
        "        tier: low\n"
        "        max_loops: 3\n"
        "        rules:\n"
        "          - {if: {verdict: rework}, goto: writer}\n"
        "          - {else: true, continue: true}\n")
    _write_fake_config(tmp_path)
    change = _make_change(tmp_path)

    writer_prompts: list[str] = []
    review_count = [0]
    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    class _Runner:
        def __init__(self, name: str) -> None:
            self._name = name

        async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
            if self._name == "writer":
                writer_prompts.append(
                    invocation.initial_messages[0].content[0].text)
                yield {"type": "agent_output",
                       "payload": {"status": "Success", "outcome": "wrote"}}
            else:
                verdict = "rework" if review_count[0] == 0 else "approve"
                review_count[0] += 1
                yield {"type": "agent_output",
                       "payload": {"status": "Success", "outcome": "reviewed",
                                   "verdict": verdict}}

        async def interrupt(self) -> None:
            pass

    class _Provider:
        name = "fake"
        model = "m"

        def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
            return _Runner(invocation.agent_def.name)

    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda _m: _Provider())

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED
    # Writer ran twice: the fresh entry (lap 1) and the post-bounce re-entry (lap 2).
    assert len(writer_prompts) == 2
    assert "lap=1" in writer_prompts[0]
    assert "lap=2" in writer_prompts[1]


@pytest.mark.anyio
async def test_step_completed_payload_carries_rule_label(tmp_path: Path) -> None:
    """A rule-matched (branch goto) completion emits the rule's label verbatim."""
    wf = tmp_path / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - name: gate\n"
        "        rules:\n"
        '          - {else: true, goto: done, label: "heading out"}\n'
        "  - name: done\n"
        "    steps: []\n"
    )
    _write_fake_config(tmp_path)
    change = _make_change(tmp_path)
    _register_fake({"status": "Success"})

    events: list[dict] = []
    result = await run_change(change, on_event=events.append)
    assert result is RunResult.COMPLETED

    completed = next(
        e for e in events
        if e["type"] == "step_completed" and e["payload"]["step"] == "gate"
    )
    assert completed["payload"]["action"]["verb"] == "goto"
    assert completed["payload"]["label"] == "heading out"


@pytest.mark.anyio
async def test_step_completed_continue_default_label_is_none(tmp_path: Path) -> None:
    """The continue-on-success default (match.rule is None) emits label: None."""
    change, _, _ = _seed_minimal_workflow(tmp_path)
    _register_fake({"status": "Success"})

    events: list[dict] = []
    result = await run_change(change, on_event=events.append)
    assert result is RunResult.COMPLETED

    completed = next(e for e in events if e["type"] == "step_completed")
    assert completed["payload"]["action"] == {"verb": "continue"}
    assert completed["payload"]["label"] is None


@pytest.mark.anyio
async def test_step_completed_unlabeled_rule_emits_none(tmp_path: Path) -> None:
    """A rule that fires but carries no label emits label: None."""
    wf = tmp_path / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - name: gate\n"
        "        rules:\n"
        "          - {else: true, goto: done}\n"
        "  - name: done\n"
        "    steps: []\n"
    )
    _write_fake_config(tmp_path)
    change = _make_change(tmp_path)
    _register_fake({"status": "Success"})

    events: list[dict] = []
    result = await run_change(change, on_event=events.append)
    assert result is RunResult.COMPLETED

    completed = next(
        e for e in events
        if e["type"] == "step_completed" and e["payload"]["step"] == "gate"
    )
    assert completed["payload"]["action"]["verb"] == "goto"
    assert completed["payload"]["label"] is None


@pytest.mark.anyio
async def test_step_started_payload_carries_label_and_resumed(tmp_path: Path) -> None:
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
        "      - {name: coder, file: agents/coder.md, tier: low, label: implementing}\n"
        "      - name: reviewer\n"
        "        file: agents/reviewer.md\n"
        "        tier: low\n"
        "        rules:\n"
        "          - {if: {verdict: Reject}, resume: coder, feedback: \"findings:\"}\n"
        "          - {else: true, continue: true}\n"
    )
    _write_fake_config(tmp_path)
    change = _make_change(tmp_path)

    from imp.providers import resolve
    resolve.reset_providers_for_tests()
    reviewer_calls = {"n": 0}

    class _Runner:
        async def run(self, invocation: AgentInvocation):
            _persist_initial(invocation)
            if invocation.agent_def.name == "reviewer":
                reviewer_calls["n"] += 1
                verdict = "Reject" if reviewer_calls["n"] == 1 else "Accept"
                yield {"type": "agent_output",
                       "payload": {"outcome": "ok", "status": "Success", "verdict": verdict}}
            else:
                yield {"type": "agent_output", "payload": {"outcome": "ok", "status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _Provider:
        name = "fake"
        model = "m"

        def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
            return _Runner()

    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda _m: _Provider())

    events: list[dict] = []
    result = await run_change(change, on_event=events.append)
    assert result is RunResult.COMPLETED

    coder_started = [e for e in events
                     if e["type"] == "step_started"
                     and e["payload"].get("step") == "coder"]
    assert len(coder_started) == 2
    assert all(e["payload"]["label"] == "implementing" for e in coder_started)
    assert coder_started[0]["payload"]["resumed"] is False
    assert coder_started[1]["payload"]["resumed"] is True
    # A replay-resume (the coder transcript was persisted, so `keep` is True)
    # is a continuation, not a re-run: restarted is False on both entries.
    assert coder_started[0]["payload"]["restarted"] is False
    assert coder_started[1]["payload"]["restarted"] is False


@pytest.mark.anyio
async def test_step_started_restarted_true_when_resumed_transcript_empty(tmp_path: Path) -> None:
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
        "      - {name: coder, file: agents/coder.md, tier: low, label: implementing}\n"
        "      - name: reviewer\n"
        "        file: agents/reviewer.md\n"
        "        tier: low\n"
        "        rules:\n"
        "          - {if: {verdict: Reject}, resume: coder, feedback: \"findings:\"}\n"
        "          - {else: true, continue: true}\n"
    )
    _write_fake_config(tmp_path)
    change = _make_change(tmp_path)

    from imp.providers import resolve
    resolve.reset_providers_for_tests()
    reviewer_calls = {"n": 0}

    class _Runner:
        async def run(self, invocation: AgentInvocation):
            # NOTE: coder deliberately does NOT persist its transcript, so the
            # later resume:coder entry sees an empty transcript (keep False).
            if invocation.agent_def.name == "reviewer":
                _persist_initial(invocation)
                reviewer_calls["n"] += 1
                verdict = "Reject" if reviewer_calls["n"] == 1 else "Accept"
                yield {"type": "agent_output",
                       "payload": {"outcome": "ok", "status": "Success", "verdict": verdict}}
            else:
                yield {"type": "agent_output", "payload": {"outcome": "ok", "status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _Provider:
        name = "fake"
        model = "m"

        def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
            return _Runner()

    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda _m: _Provider())

    events: list[dict] = []
    result = await run_change(change, on_event=events.append)
    assert result is RunResult.COMPLETED

    coder_started = [e for e in events
                     if e["type"] == "step_started"
                     and e["payload"].get("step") == "coder"]
    assert len(coder_started) == 2
    assert coder_started[0]["payload"]["restarted"] is False   # fresh entry
    assert coder_started[1]["payload"]["restarted"] is True    # empty-transcript resume


# ── sandbox policy rebuild on project_root flip ─────────────────────────────


class _FlipRunner:
    def __init__(self, sink: list[str], wt: Path) -> None:
        self._sink = sink
        self._wt = wt

    async def run(self, invocation: AgentInvocation) -> AsyncGenerator[Event, None]:
        self._sink.append(invocation.sandbox_policy.worktree)
        if len(self._sink) == 1:                       # first step = go-live flip
            invocation.change.project_root = self._wt
        yield {"type": "agent_output",
               "payload": {"outcome": "ok", "status": "Success"}}

    async def interrupt(self) -> None:
        pass


class _FlipProvider:
    name = "fake"

    def __init__(self, sink: list[str], wt: Path, model: str = "m") -> None:
        self._sink = sink
        self._wt = wt
        self.model = model

    def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
        return _FlipRunner(self._sink, self._wt)


def _register_flip(sink: list[str], wt: Path) -> None:
    from imp.providers import resolve
    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "fake", env_check=lambda: True,
        factory=lambda model: _FlipProvider(sink, wt, model=model),
    )


def _seed_two_step_workflow(root: Path) -> Change:
    wf_dir = root / "imp" / "workflows" / "imp"
    (wf_dir / "agents").mkdir(parents=True)
    (wf_dir / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\n"
        "go {{ change.id }}\n"
    )
    (wf_dir / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - {name: s1, file: agents/writer.md, tier: low}\n"
        "      - {name: s2, file: agents/writer.md, tier: low}\n"
    )
    _write_fake_config(root)
    return _make_change(root)


@pytest.mark.anyio
@pytest.mark.real_sandbox_policy
async def test_sandbox_policy_rebuilt_after_project_root_flip(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import imp.engine.executor as ex
    monkeypatch.setattr(ex, "sandbox_available", lambda: True)

    change = _seed_two_step_workflow(tmp_path)
    wt = tmp_path / ".imp" / "worktrees" / change.id
    wt.mkdir(parents=True)
    sink: list[str] = []
    _register_flip(sink, wt)

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED

    # Step 1 ran under the control-root policy; the flip mid-step rebuilds the
    # policy so step 2 binds the worktree.
    assert sink[0] == str(tmp_path)
    assert sink[1] == str(wt)


@pytest.mark.anyio
async def test_continue_writes_target_step_label(tmp_path: Path) -> None:
    """A forward Continue persists the NEXT step's label-or-name into
    state.step_label, derived from the labeled step the cursor lands on."""
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
        "  - name: only\n"
        "    steps:\n"
        "      - name: w\n"
        "        file: agents/writer.md\n"
        "        label: writing the thing\n"
        "        tier: low\n"
        "        rules:\n"
        "          - {else: true, continue: true}\n"
        "      - name: check\n"
        "        label: second gate\n"
        "        command: 'exit 7'\n"
        "        rules:\n"
        "          - {else: true, pause: true}\n"
    )
    _write_fake_config(tmp_path)
    import subprocess
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    change = _make_change(tmp_path)
    _register_fake({"status": "Success"})

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.PAUSED
    state = State.load(change.state_dir)
    assert state.step == "check"
    assert state.step_label == "second gate"   # the navigated-to step's label


@pytest.mark.anyio
async def test_complete_clears_step_label(tmp_path: Path) -> None:
    """Running off the last step clears step_label to None beside state.step.
    Two steps so a non-None step_label (the second step's label, written by the
    first step's Continue) exists before _complete — making the clear falsifiable."""
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
        "  - name: only\n"
        "    steps:\n"
        "      - name: w\n"
        "        file: agents/writer.md\n"
        "        label: writing the thing\n"
        "        tier: low\n"
        "        rules:\n"
        "          - {else: true, continue: true}\n"
        "      - name: w2\n"
        "        file: agents/writer.md\n"
        "        label: second writer\n"
        "        tier: low\n"
        "        rules:\n"
        "          - {else: true, continue: true}\n"
    )
    _write_fake_config(tmp_path)
    change = _make_change(tmp_path)
    _register_fake({"status": "Success"})

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED
    state = State.load(change.state_dir)
    assert state.step is None
    assert state.step_label is None


@pytest.mark.anyio
async def test_goto_writes_target_step_label(tmp_path: Path) -> None:
    """A Goto persists the TARGET step's label-or-name into state.step_label."""
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
        "  - name: only\n"
        "    steps:\n"
        "      - name: src\n"
        "        label: source step\n"
        "        file: agents/writer.md\n"
        "        tier: low\n"
        "        rules:\n"
        "          - {else: true, goto: dst}\n"
        "      - name: dst\n"
        "        label: destination step\n"
        "        file: agents/writer.md\n"
        "        tier: low\n"
        "        rules:\n"
        "          - {else: true, pause: true}\n"
    )
    _write_fake_config(tmp_path)
    change = _make_change(tmp_path)
    _register_fake({"status": "Success"})

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.PAUSED
    state = State.load(change.state_dir)
    assert state.step == "dst"
    assert state.step_label == "destination step"


def test_step_started_payload_carries_command_only_for_script() -> None:
    from imp.engine.executor import _step_started_payload
    from imp.engine.types import Pause, SourceLoc, Step
    loc = SourceLoc(file=Path("w.yaml"), line=1)
    script = Step(name="gate", phase="verify", kind="script", rules=(),
                  on_max_retries=Pause(), max_loops=1, source=loc,
                  command="make check")
    agent = Step(name="spec", phase="design", kind="agent", rules=(),
                 on_max_retries=Pause(), max_loops=1, source=loc)
    state = State(id="t", step=None, step_attempt=0, status="running")
    assert _step_started_payload(script, state)["command"] == "make check"
    assert "command" not in _step_started_payload(agent, state)


@pytest.mark.anyio
async def test_executor_passes_autonomous_to_prompt(tmp_path: Path) -> None:
    wf = tmp_path / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\n"
        "autonomous={{ run.autonomous }}\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n  - name: only\n    steps:\n"
        "      - {name: w, file: agents/writer.md, tier: low}\n"
    )
    _write_fake_config(tmp_path)
    change = _make_change(tmp_path)
    sink: list = []
    _register_invocation_capturing({"status": "Success"}, sink)

    result = await run_change(
        change, on_event=lambda _e: None, autonomous=True,
    )
    assert result is RunResult.COMPLETED
    assert any("autonomous=True" in i.initial_messages[0].content[0].text for i in sink)


@pytest.mark.anyio
async def test_change_home_renders_worktree_relative(tmp_path: Path) -> None:
    wf = tmp_path / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\n"
        "home={{ change.home }}\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - {name: w, file: agents/writer.md, tier: low}\n"
    )
    _write_fake_config(tmp_path)
    change = _make_change(tmp_path)
    sink: list = []
    _register_invocation_capturing({"status": "Success"}, sink)

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED

    inv = sink[0]
    assert inv.change_context.change_home == ".imp/changes/test-change"
    assert "home=.imp/changes/test-change" in inv.initial_messages[0].content[0].text
    # worktree-anchored: relative, never absolute, never a parent escape.
    assert not inv.change_context.change_home.startswith("/")
    assert ".." not in inv.change_context.change_home


@pytest.mark.anyio
async def test_route_label_threads_to_next_step_started(tmp_path: Path) -> None:
    """A labeled rule on `writer` makes the NEXT step's step_started carry
    route_label; an unlabeled continue leaves it absent, and consume-on-read
    prevents the reason bleeding into the step after that."""
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
        "      - name: writer\n"
        "        file: agents/writer.md\n"
        "        tier: low\n"
        "        rules:\n"
        "          - {else: true, continue: true, label: \"addressing review feedback\"}\n"
        "      - {name: second, file: agents/writer.md, tier: low}\n"
        "      - {name: third, file: agents/writer.md, tier: low}\n"
    )
    _write_fake_config(tmp_path)
    change = _make_change(tmp_path)
    _register_fake({"status": "Success"})

    events: list[dict] = []
    result = await run_change(change, on_event=events.append)
    assert result is RunResult.COMPLETED

    started = {
        e["payload"]["step"]: e["payload"]
        for e in events if e["type"] == "step_started"
    }
    # The emitter (writer) entered with no prior labeled route → no suffix.
    assert "route_label" not in started["writer"]
    # The labeled route carries the reason onto the TARGET step's start.
    assert started["second"]["route_label"] == "addressing review feedback"
    # Consume-on-read: the unlabeled continue off `second` leaves `third` clean.
    assert "route_label" not in started["third"]


@pytest.mark.anyio
async def test_continue_renders_templated_target_label(tmp_path: Path) -> None:
    from imp.cli.change_row import _status_segment

    wf = tmp_path / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "vars:\n  n: {type: int, default: 5}\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - name: w\n"
        "        file: agents/writer.md\n"
        "        label: writing the thing\n"
        "        tier: low\n"
        "        rules:\n"
        "          - {else: true, continue: true}\n"
        "      - name: check\n"
        "        label: 'processing {{ vars.n }}'\n"
        "        command: 'exit 7'\n"
        "        rules:\n"
        "          - {else: true, pause: true}\n"
    )
    _write_fake_config(tmp_path)
    import subprocess
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    change = _make_change(tmp_path)
    _register_fake({"status": "Success"})

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.PAUSED
    state = State.load(change.state_dir)
    assert state.step == "check"
    assert state.step_label == "processing 5"
    seg = _status_segment(state.status, state.step, state.step_label)
    assert "{{" not in seg
    assert "processing 5" in seg


@pytest.mark.anyio
async def test_goto_renders_templated_target_label(tmp_path: Path) -> None:
    wf = tmp_path / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "vars:\n  n: {type: int, default: 5}\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - name: src\n"
        "        label: source step\n"
        "        file: agents/writer.md\n"
        "        tier: low\n"
        "        rules:\n"
        "          - {else: true, goto: dst}\n"
        "      - name: dst\n"
        "        label: 'arrived at {{ vars.n }}'\n"
        "        file: agents/writer.md\n"
        "        tier: low\n"
        "        rules:\n"
        "          - {else: true, pause: true}\n"
    )
    _write_fake_config(tmp_path)
    change = _make_change(tmp_path)
    _register_fake({"status": "Success"})

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.PAUSED
    state = State.load(change.state_dir)
    assert state.step == "dst"
    assert state.step_label == "arrived at 5"


@pytest.mark.anyio
async def test_first_run_overlays_project_skills_into_snapshot_user(
    tmp_path: Path,
) -> None:
    """At snapshot creation, project_root/imp/skills/*.md land in the snapshot's
    skills/user/; resume does not re-run the overlay."""
    change, _, _ = _seed_minimal_workflow(tmp_path)
    _register_fake({"status": "Success"})
    (tmp_path / "imp" / "skills").mkdir(parents=True, exist_ok=True)
    (tmp_path / "imp" / "skills" / "mine.md").write_text("# Mine\nv1\n")

    assert await run_change(change, on_event=lambda _e: None) is RunResult.COMPLETED
    overlaid = change.change_dir / "workflow" / "skills" / "user" / "mine.md"
    assert overlaid.read_text() == "# Mine\nv1\n"

    # Resume must not re-overlay: change the source, run again, snapshot unchanged.
    (tmp_path / "imp" / "skills" / "mine.md").write_text("# Mine\nv2\n")
    await run_change(change, on_event=lambda _e: None)
    assert overlaid.read_text() == "# Mine\nv1\n"


@pytest.mark.anyio
async def test_run_change_persists_configured_max_reincarnations(tmp_path):
    """run_change copies config.context_max_reincarnations() onto State at run
    start (config is in scope only here), like resolved_tiers."""
    change, _, _ = _seed_minimal_workflow(tmp_path)
    # Overwrite the project layer with a NON-default cap so the assertion is
    # falsifiable (5 != the default 2).
    (tmp_path / "imp" / "project.yaml").write_text(
        "default-workflow: imp\ncontext:\n  max_reincarnations: 5\n")
    _register_fake({"status": "Success"})

    await run_change(change, on_event=lambda _e: None)

    assert State.load(change.state_dir).max_reincarnations == 5


@pytest.mark.anyio
async def test_step_reincarnates_on_context_window_then_pauses_at_cap(tmp_path):
    """A step whose runner hits the hard window error reincarnates fresh (Tier B
    backstop) up to the per-session cap, then pauses — emitting one
    `reincarnating` event and stashing the prior transcript at .attempt-1."""
    from imp.providers import resolve

    change, phase, step = _seed_minimal_workflow(tmp_path)
    (tmp_path / "imp" / "project.yaml").write_text(
        "default-workflow: imp\ncontext:\n  max_reincarnations: 1\n")

    class _CtxWindowRunner:
        async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
            _persist_initial(invocation)   # non-empty transcript -> stashable
            yield {"type": "error", "payload": {
                "category": "context_window", "kind": "prompt_too_long",
                "message": "prompt is too long"}}

        async def interrupt(self) -> None:
            pass

    class _CtxWindowProvider:
        name = "fake"

        def __init__(self, model: str = "m") -> None:
            self.model = model

        def create_runner(self, invocation: AgentInvocation) -> _CtxWindowRunner:
            return _CtxWindowRunner()

    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "fake", env_check=lambda: True,
        factory=lambda model: _CtxWindowProvider(model))

    events: list[dict] = []
    result = await run_change(change, on_event=events.append)

    assert result is RunResult.PAUSED
    reincarnating = [e for e in events if e["type"] == "reincarnating"]
    assert len(reincarnating) == 1
    assert reincarnating[0]["payload"]["reason"] == "context window exceeded"
    assert (change.change_dir / "steps" / phase / f"{step}.attempt-1").exists()


@pytest.mark.anyio
async def test_run_change_threads_checkpoint_threshold_to_invocation(tmp_path):
    """run_change re-derives config.context_checkpoint_threshold() onto State on
    every entry (transient, not persisted) and the executor carries it onto each
    AgentInvocation, so the runner's Tier A watch sees the configured value."""
    change, _, _ = _seed_minimal_workflow(tmp_path)
    # Non-default threshold so the assertion is falsifiable (0.65 != default 0.8).
    (tmp_path / "imp" / "project.yaml").write_text(
        "default-workflow: imp\ncontext:\n  checkpoint_threshold: 0.65\n")
    sink: list = []
    _register_invocation_capturing({"status": "Success"}, sink)

    await run_change(change, on_event=lambda _e: None)

    assert sink and sink[0].checkpoint_threshold == 0.65


@pytest.mark.anyio
async def test_step_reincarnates_on_handoff_then_pauses_at_cap(tmp_path):
    """A step whose runner captures a Tier A handoff reincarnates fresh seeded
    from it (up to the per-session cap), emitting one `reincarnating` event with
    the threshold reason and stashing the prior transcript; at the cap it pauses."""
    from imp.engine.reincarnation import handoff_path_for
    from imp.providers import resolve

    change, phase, step = _seed_minimal_workflow(tmp_path)
    (tmp_path / "imp" / "project.yaml").write_text(
        "default-workflow: imp\ncontext:\n  max_reincarnations: 1\n")

    class _HandoffRunner:
        async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
            _persist_initial(invocation)   # non-empty transcript -> stashable
            hp = handoff_path_for(invocation.transcript_dir)
            hp.write_text("# handoff\n")
            yield {"type": "handoff", "payload": {"handoff_path": str(hp)}}

        async def interrupt(self) -> None:
            pass

    class _HandoffProvider:
        name = "fake"

        def __init__(self, model: str = "m") -> None:
            self.model = model

        def create_runner(self, invocation: AgentInvocation) -> _HandoffRunner:
            return _HandoffRunner()

    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "fake", env_check=lambda: True,
        factory=lambda model: _HandoffProvider(model))

    events: list[dict] = []
    result = await run_change(change, on_event=events.append)

    assert result is RunResult.PAUSED
    reincarnating = [e for e in events if e["type"] == "reincarnating"]
    assert len(reincarnating) == 1
    assert reincarnating[0]["payload"]["reason"] == "context threshold crossed"
    assert (change.change_dir / "steps" / phase / f"{step}.attempt-1").exists()
    cap_event = next(e for e in events if e["type"] == "step_completed")
    assert cap_event["payload"]["action"]["verb"] == "pause"


@pytest.mark.anyio
async def test_step_handoff_missing_note_degrades_to_reactive_reason(tmp_path):
    """When the runner reports a Tier A handoff but the note file is absent, the
    engine treats it as a FAILED handoff: it reincarnates from the stashed
    transcript alone and emits the DISTINCT reason
    'handoff failed — recovering from transcript' — never the clean threshold
    reason. The runner always reports handoff_path; the engine arbitrates."""
    from imp.engine.reincarnation import handoff_path_for
    from imp.providers import resolve

    change, phase, step = _seed_minimal_workflow(tmp_path)
    (tmp_path / "imp" / "project.yaml").write_text(
        "default-workflow: imp\ncontext:\n  max_reincarnations: 1\n")

    class _HandoffNoNoteRunner:
        async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
            _persist_initial(invocation)   # non-empty transcript -> stashable
            hp = handoff_path_for(invocation.transcript_dir)
            # Note deliberately NOT written: the handoff turn failed to produce it.
            yield {"type": "handoff", "payload": {"handoff_path": str(hp)}}

        async def interrupt(self) -> None:
            pass

    class _HandoffNoNoteProvider:
        name = "fake"

        def __init__(self, model: str = "m") -> None:
            self.model = model

        def create_runner(self, invocation: AgentInvocation) -> _HandoffNoNoteRunner:
            return _HandoffNoNoteRunner()

    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "fake", env_check=lambda: True,
        factory=lambda model: _HandoffNoNoteProvider(model))

    events: list[dict] = []
    result = await run_change(change, on_event=events.append)

    assert result is RunResult.PAUSED
    reincarnating = [e for e in events if e["type"] == "reincarnating"]
    assert len(reincarnating) == 1
    assert reincarnating[0]["payload"]["reason"] == \
        "handoff failed — recovering from transcript"
    assert (change.change_dir / "steps" / phase / f"{step}.attempt-1").exists()


@pytest.mark.anyio
async def test_resume_keep_path_feedback_ends_with_backstop(tmp_path: Path) -> None:
    """Kept-transcript resume (retry_wrapper path): the feedback delivered to the
    resumed schema'd agent preserves the source feedback and ends with the
    completion backstop."""
    change = _seed_back_edge_workflow(
        tmp_path, verb="resume", feedback="reviewer findings:")
    runs: list = []
    _register_capturing(lambda _n: ("output", {"status": "Success"}),
                        ["rework", "approve"], runs)
    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED
    coder_feedback = [fb for (aid, _bal, fb) in runs if aid.endswith("/coder")]
    assert coder_feedback[0] is None                     # fresh entry: no wrapper
    assert coder_feedback[1] is not None
    assert "reviewer findings:" in coder_feedback[1]     # source feedback preserved
    assert coder_feedback[1].endswith(COMPLETION_BACKSTOP)


@pytest.mark.anyio
async def test_bare_resume_backstop_rides_on_prior_attempts_preamble(tmp_path: Path) -> None:
    """A bare (feedback=None) resume back-edge is still a rework re-entry: the
    engine injects a prior-attempts preamble as wrapper_text, so the schema'd
    coder's resumed feedback is non-empty and ends with the backstop."""
    change = _seed_back_edge_workflow(tmp_path, verb="resume")  # feedback=None
    runs: list = []
    _register_capturing(lambda _n: ("output", {"status": "Success"}),
                        ["rework", "approve"], runs)
    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED
    coder_feedback = [fb for (aid, _bal, fb) in runs if aid.endswith("/coder")]
    assert coder_feedback[0] is None                          # fresh entry: no wrapper
    assert coder_feedback[1] is not None
    assert "Prior attempts at this step" in coder_feedback[1]  # engine preamble
    assert coder_feedback[1].endswith(COMPLETION_BACKSTOP)


@pytest.mark.anyio
async def test_resume_handoff_framed_feedback_ends_with_backstop(tmp_path: Path) -> None:
    """Regression: a handoff-framed feedback string still gets the backstop appended."""
    change = _seed_back_edge_workflow(
        tmp_path, verb="resume",
        feedback="Your work is done for now — hand back to the reviewer.")
    runs: list = []
    _register_capturing(lambda _n: ("output", {"status": "Success"}),
                        ["rework", "approve"], runs)
    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED
    coder_feedback = [fb for (aid, _bal, fb) in runs if aid.endswith("/coder")]
    assert coder_feedback[1].endswith(COMPLETION_BACKSTOP)


@pytest.mark.anyio
async def test_goto_fresh_path_feedback_carries_backstop(tmp_path: Path) -> None:
    """Fresh-transcript resume (feedback_lead path): the backstop rides in the
    relocated first user message of the freshly-restarted schema'd agent."""
    change = _seed_back_edge_workflow(
        tmp_path, verb="goto", feedback="Address these findings:")
    invs: list[AgentInvocation] = []

    class _Rec:
        async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
            invs.append(invocation)
            _persist_initial(invocation)
            if invocation.agent_def.name == "reviewer":
                reviewer_n = len([i for i in invs if i.agent_def.name == "reviewer"])
                verdict = "rework" if reviewer_n == 1 else "approve"
                yield {"type": "agent_output",
                       "payload": {"outcome": "ok", "verdict": verdict, "status": "Success"}}
            else:
                yield {"type": "agent_output",
                       "payload": {"outcome": "ok", "status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _Prov:
        name = "fake"

        def __init__(self, model: str = "m") -> None:
            self.model = model

        def create_runner(self, invocation: AgentInvocation) -> _Rec:
            return _Rec()

    from imp.providers import resolve
    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda model: _Prov(model))

    await run_change(change, on_event=lambda _e: None, headless=True)

    coder_invs = [i for i in invs if i.agent_def.name == "coder"]
    assert len(coder_invs) == 2
    reworked = coder_invs[1].initial_messages[0].content[0].text
    assert "Address these findings:" in reworked   # source feedback preserved
    assert COMPLETION_BACKSTOP in reworked         # backstop applied once
    assert reworked.count(COMPLETION_BACKSTOP) == 1


@pytest.mark.anyio
async def test_backward_gate_bounce_emits_step_transition(tmp_path: Path) -> None:
    """A gate whose `else` resumes an earlier step emits exactly one
    `step_transition` event naming source, target, the backward flag, and a
    reason summarized from the gate feedback — and it is logged after the gate's
    own step_completed (renders at the bounce point, not the bottom)."""
    import subprocess

    wf = tmp_path / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "lead.md").write_text(
        "---\nname: lead\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 10}\n"
        "phases:\n"
        "  - name: work\n"
        "    steps:\n"
        "      - name: lead\n"
        "        file: agents/lead.md\n"
        "        tier: low\n"
        "        rules:\n"
        "          - {else: true, continue: true}\n"
        "      - name: gate\n"
        "        command: 'echo 50 coverage issues >&2; exit 1'\n"
        "        max_loops: 1\n"
        "        rules:\n"
        "          - {if: {exit: 0}, continue: true}\n"
        "          - {else: true, resume: lead}\n"
    )
    _write_fake_config(tmp_path)
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    change = _make_change(tmp_path)

    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    class _Runner:
        async def run(self, invocation: AgentInvocation):
            _persist_initial(invocation)
            yield {"type": "agent_output",
                   "payload": {"outcome": "ok", "status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _Provider:
        name = "fake"
        model = "m"

        def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
            return _Runner()

    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda _m: _Provider())

    await run_change(change, on_event=lambda _e: None)

    events = list(change.event_log().read())
    trans = [e for e in events if e["type"] == "step_transition"]
    assert len(trans) == 1
    p = trans[0]["payload"]
    assert p["from_step"] == "gate"
    assert p["target_step"] == "lead"
    assert p["backward"] is True
    assert p["reason"] is not None and "gate" in p["reason"].lower()

    types = [e["type"] for e in events]
    ti = types.index("step_transition")
    gate_completions = [
        i for i, e in enumerate(events)
        if e["type"] == "step_completed" and e["payload"]["step"] == "gate"]
    assert any(i < ti for i in gate_completions)


@pytest.mark.anyio
async def test_forward_only_run_emits_no_step_transition(tmp_path: Path) -> None:
    """A run with only forward Continue/goto edges emits no `step_transition`."""
    import subprocess

    wf = tmp_path / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "lead.md").write_text(
        "---\nname: lead\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 10}\n"
        "phases:\n"
        "  - name: work\n"
        "    steps:\n"
        "      - name: lead\n"
        "        file: agents/lead.md\n"
        "        tier: low\n"
        "        rules:\n"
        "          - {else: true, continue: true}\n"
        "      - name: gate\n"
        "        command: 'exit 0'\n"
        "        rules:\n"
        "          - {if: {exit: 0}, continue: true}\n"
        "          - {else: true, resume: lead}\n"
    )
    _write_fake_config(tmp_path)
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    change = _make_change(tmp_path)

    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    class _Runner:
        async def run(self, invocation: AgentInvocation):
            _persist_initial(invocation)
            yield {"type": "agent_output",
                   "payload": {"outcome": "ok", "status": "Success"}}

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
    events = list(change.event_log().read())
    assert not any(e["type"] == "step_transition" for e in events)


@pytest.mark.anyio
async def test_self_goto_retry_emits_no_step_transition(tmp_path: Path) -> None:
    """A gate that resumes itself (self-goto, target_idx == idx) emits no step_transition."""
    import subprocess

    wf = tmp_path / "imp" / "workflows" / "imp"
    wf.mkdir(parents=True)
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 10}\n"
        "phases:\n"
        "  - name: work\n"
        "    steps:\n"
        "      - name: gate\n"
        "        command: 'exit 1'\n"
        "        max_loops: 3\n"
        "        rules:\n"
        "          - {if: {exit: 0}, continue: true}\n"
        "          - {else: true, goto: gate}\n"
    )
    _write_fake_config(tmp_path)
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    change = _make_change(tmp_path)

    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    class _Runner:
        async def run(self, invocation: AgentInvocation):
            _persist_initial(invocation)
            yield {"type": "agent_output",
                   "payload": {"outcome": "ok", "status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _Provider:
        name = "fake"
        model = "m"

        def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
            return _Runner()

    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda _m: _Provider())

    await run_change(change, on_event=lambda _e: None)

    events = list(change.event_log().read())
    assert not any(e["type"] == "step_transition" for e in events)

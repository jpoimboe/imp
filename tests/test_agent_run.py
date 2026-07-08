from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from imp.agents.reflow import reflow_markdown
from imp.agents.runner import ChangeContext, Message, TextBlock
from imp.cli.signals import InterruptCoordinator
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _agent(name: str = "rev", max_tool_calls=None, subagents=(),
           tools=frozenset({"complete_step"})) -> AgentDef:
    return AgentDef(
        name=name, description="", tools=frozenset(tools),
        subagents=tuple(subagents),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1)),
        prompt_template_source="body", source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1),
        max_tool_calls=max_tool_calls)


def _cc(tmp: Path) -> ChangeContext:
    return ChangeContext(title="t", project_root=tmp,
                         workflow_dir=tmp / "wf")


def test_error_info_and_agent_run_result_live_in_agent_run() -> None:
    from imp.engine.agent_run import AgentRunResult, ErrorInfo, SubagentSuspended

    err = ErrorInfo(category="rate_limit", kind="429")
    assert err.category == "rate_limit"
    assert err.kind == "429"

    res = AgentRunResult(output={"status": "Success"}, errored=False,
                         paused=False, fatal=False, delay=None)
    assert res.output == {"status": "Success"}
    assert res.error is None  # default

    assert issubclass(SubagentSuspended, Exception)


def test_executor_reexports_agent_run_result() -> None:
    # AgentRunResult stays importable from executor (it is the return annotation
    # of _run_agent_invocation). Both classes now live in agent_run.
    # NOTE: do NOT assert ErrorInfo is importable from executor — Task 7 removes
    # the drain loop, after which executor no longer references ErrorInfo by name
    # and ruff F401 forbids keeping the dead import. Nothing imports ErrorInfo
    # from executor (verified by grep), so there is no re-export to preserve.
    from imp.engine.executor import AgentRunResult
    assert AgentRunResult.__module__ == "imp.engine.agent_run"


def test_build_agent_invocation_appends_completion_instruction(tmp_path: Path) -> None:
    from imp.engine.agent_run import _CORE_HEAD, build_agent_invocation

    inv = build_agent_invocation(
        agent_def=_agent(), provider_name="anthropic", model_name="m",
        rendered_system_prompt="RENDERED BODY",
        initial_messages=(), change_context=_cc(tmp_path),
        user_chat_input_fn=None,
        transcript_dir=tmp_path / "td",
        events_path=tmp_path / "e.jsonl", subagent_defs={}, tiers=(),
        max_tool_calls=7, agent_id="sub:abc", event_log=None,
        change_state=None, first_event_parent_id="tc-1",
        resolved_tiers=None, coordinator=None, subagent_depth=0)

    # System prompt is role-only; completion instruction rides in the user turn.
    assert inv.system_prompt.startswith(reflow_markdown(_CORE_HEAD))
    assert "RENDERED BODY" not in inv.system_prompt
    assert "complete_step" not in inv.system_prompt
    task = inv.initial_messages[0].content[0].text
    assert "RENDERED BODY" in task
    assert "complete_step" in task
    # Continuation fn is wired for top-level steps.
    assert inv.continuation_fn is not None
    # Ceiling resolved (omission #3 closed).
    assert inv.max_tool_calls == 7
    # Threaded fields land on the invocation.
    assert inv.subagent_depth == 0
    assert inv.agent_id == "sub:abc"
    assert inv.first_event_parent_id == "tc-1"
    assert inv.user_input_fn is None  # non-interactive


def test_build_agent_invocation_injects_bash_paths_note_when_bash_present(
    tmp_path: Path,
) -> None:
    # An agent holding `bash` gets the relative-paths note in its role-only
    # system prompt (not between the rendered body and the completion contract),
    # so every bash-capable agent learns the rule without each prompt re-pasting it.
    from imp.engine.agent_run import _CORE_HEAD, build_agent_invocation

    inv = build_agent_invocation(
        agent_def=_agent(tools={"complete_step", "bash"}),
        provider_name="anthropic", model_name="m",
        rendered_system_prompt="RENDERED BODY",
        initial_messages=(), change_context=_cc(tmp_path),
        user_chat_input_fn=None,
        transcript_dir=tmp_path / "td",
        events_path=tmp_path / "e.jsonl", subagent_defs={}, tiers=(),
        max_tool_calls=None, agent_id="step:p/s", event_log=None,
        change_state=None, first_event_parent_id=None,
        resolved_tiers=None, coordinator=None)

    assert inv.system_prompt.startswith(reflow_markdown(_CORE_HEAD))
    assert "RENDERED BODY" not in inv.system_prompt      # body moved to user turn
    assert "IMP_PROJECT_ROOT" in inv.system_prompt       # bash note stays in system
    assert "relative to the project root" in inv.system_prompt
    task = inv.initial_messages[0].content[0].text
    assert "RENDERED BODY" in task
    assert "complete_step" in task


def test_build_agent_invocation_omits_bash_paths_note_without_bash(
    tmp_path: Path,
) -> None:
    # No `bash` tool -> no note (the rule is irrelevant to file-tool-only agents).
    from imp.engine.agent_run import build_agent_invocation

    inv = build_agent_invocation(
        agent_def=_agent(),  # tools = {complete_step}
        provider_name="anthropic", model_name="m",
        rendered_system_prompt="RENDERED BODY",
        initial_messages=(), change_context=_cc(tmp_path),
        user_chat_input_fn=None,
        transcript_dir=tmp_path / "td",
        events_path=tmp_path / "e.jsonl", subagent_defs={}, tiers=(),
        max_tool_calls=None, agent_id="step:p/s", event_log=None,
        change_state=None, first_event_parent_id=None,
        resolved_tiers=None, coordinator=None)

    assert "IMP_PROJECT_ROOT" not in inv.system_prompt


def test_build_agent_invocation_wires_user_input_unconditionally(
    tmp_path: Path,
) -> None:
    from imp.engine.agent_run import build_agent_invocation

    inv = build_agent_invocation(
        agent_def=_agent(), provider_name="anthropic", model_name="m",
        rendered_system_prompt="BODY", initial_messages=(),
        change_context=_cc(tmp_path),
        user_chat_input_fn=lambda _p: "hi",
        transcript_dir=tmp_path / "td",
        events_path=tmp_path / "e.jsonl", subagent_defs={}, tiers=(),
        max_tool_calls=None, agent_id="step:p/s", event_log=None,
        change_state=None, first_event_parent_id=None,
        resolved_tiers=None, coordinator=None)

    assert inv.continuation_fn is not None
    # user_chat_input_fn is wired unconditionally (the wait_for_user / AWAITING_USER
    # resume input channel for any attended top-level agent).
    assert inv.user_input_fn is not None


def test_build_agent_invocation_threads_test_gates(tmp_path: Path) -> None:
    from imp.engine.agent_run import build_agent_invocation
    from imp.engine.env import TestGates

    inv = build_agent_invocation(
        agent_def=_agent(), provider_name="anthropic", model_name="m",
        rendered_system_prompt="BODY", initial_messages=(),
        change_context=_cc(tmp_path),
        user_chat_input_fn=None, transcript_dir=tmp_path / "td",
events_path=tmp_path / "e.jsonl",
        subagent_defs={}, tiers=(), max_tool_calls=None,
        agent_id="step:p/s", event_log=None, change_state=None,
        first_event_parent_id=None, resolved_tiers=None, coordinator=None,
        test_gates=TestGates(fast="make check", fast_timeout=120))
    assert inv.test_gates.fast == "make check"
    assert inv.test_gates.fast_timeout == 120


def _inv(tmp_path: Path):
    from imp.engine.agent_run import build_agent_invocation
    return build_agent_invocation(
        agent_def=_agent(), provider_name="p", model_name="m",
        rendered_system_prompt="B", initial_messages=(),
        change_context=_cc(tmp_path),
        user_chat_input_fn=None, transcript_dir=tmp_path / "td",
events_path=tmp_path / "e.jsonl",
        subagent_defs={}, tiers=(), max_tool_calls=None,
        agent_id="step:p/s", event_log=None, change_state=None,
        first_event_parent_id=None, resolved_tiers=None, coordinator=None)


class _Runner:
    def __init__(self, events, on_run=None):
        self._events = events
        self._on_run = on_run
    async def run(self, invocation):
        if self._on_run is not None:
            self._on_run()
        for e in self._events:
            yield e
    async def interrupt(self) -> None: ...


@pytest.mark.anyio
async def test_drive_invocation_success(tmp_path: Path) -> None:
    from imp.engine.agent_run import drive_invocation

    coord = InterruptCoordinator()
    runner = _Runner([{"type": "agent_output", "payload": {"status": "Success"}}])
    res = await drive_invocation(_inv(tmp_path), runner,
                                 coordinator=coord, log=None, emit_events=False)
    assert res.output == {"status": "Success"}
    assert res.errored is False and res.paused is False and res.fatal is False


@pytest.mark.anyio
async def test_drive_invocation_fatal_error(tmp_path: Path) -> None:
    from imp.engine.agent_run import drive_invocation

    coord = InterruptCoordinator()
    runner = _Runner([{"type": "error", "payload": {
        "category": "terminated", "kind": "TerminatedError", "message": "boom"}}])
    res = await drive_invocation(_inv(tmp_path), runner,
                                 coordinator=coord, log=None, emit_events=False)
    assert res.fatal is True
    assert res.error is not None and res.error.category == "terminated"


@pytest.mark.anyio
async def test_drive_invocation_auth_backs_off_below_cap(tmp_path: Path) -> None:
    from imp.engine.agent_run import drive_invocation

    coord = InterruptCoordinator()
    runner = _Runner([{"type": "error", "payload": {
        "category": "auth", "kind": "http_401", "message": "401"}}])
    res = await drive_invocation(_inv(tmp_path), runner,
                                 coordinator=coord, log=None, emit_events=False,
                                 prior_backoff_attempts=0)
    assert res.fatal is False
    assert res.delay is not None and res.delay > 0
    assert res.error is not None and res.error.category == "auth"


@pytest.mark.anyio
async def test_drive_invocation_auth_backs_off_at_high_attempts(tmp_path: Path) -> None:
    # The auth attempt cap is gone; its give-up now rides the executor/subagent
    # 1800s budget, so decide() keeps backing off (60s-capped) here instead of
    # pausing — fatal stays False and a capped delay is returned.
    from imp.engine.agent_run import drive_invocation

    coord = InterruptCoordinator()
    runner = _Runner([{"type": "error", "payload": {
        "category": "auth", "kind": "http_401", "message": "401"}}])
    res = await drive_invocation(_inv(tmp_path), runner,
                                 coordinator=coord, log=None, emit_events=False,
                                 prior_backoff_attempts=7)
    assert res.fatal is False
    assert res.delay == 60.0
    assert res.error is not None and res.error.category == "auth"


@pytest.mark.anyio
async def test_drive_invocation_backoff_on_rate_limit(tmp_path: Path) -> None:
    from imp.engine.agent_run import drive_invocation

    coord = InterruptCoordinator()
    runner = _Runner([{"type": "error", "payload": {
        "category": "rate_limit", "kind": "429", "message": "slow down"}}])
    res = await drive_invocation(_inv(tmp_path), runner,
                                 coordinator=coord, log=None, emit_events=False,
                                 prior_backoff_attempts=0)
    assert res.delay is not None and res.delay > 0
    assert res.error is not None and res.error.category == "rate_limit"


@pytest.mark.anyio
async def test_drive_invocation_coordinator_pause(tmp_path: Path) -> None:
    from imp.engine.agent_run import drive_invocation

    coord = InterruptCoordinator()

    def _trip():
        coord._should_pause = True

    # A benign event lets the loop body run once; should_pause is checked there.
    runner = _Runner([{"type": "thinking", "payload": {}}], on_run=_trip)
    res = await drive_invocation(_inv(tmp_path), runner,
                                 coordinator=coord, log=None, emit_events=False)
    assert res.paused is True


@pytest.mark.anyio
async def test_drive_invocation_pause_in_flight_wins_over_retryable_error(
    tmp_path: Path,
) -> None:
    """When a pause is already in flight, an error event must pause cleanly, NOT
    retry on its own classification. A process-group interrupt kills a provider's
    CLI child instantly, so a (sub)agent can surface a `network`/no_result_message
    teardown artifact a beat before signal_pause's interrupt() sets _cancel;
    retrying it replays an interrupted transcript (the 400 guard). Regression for
    the --tui playbook's subagent-interrupt finding.
    """
    from imp.engine.agent_run import drive_invocation

    coord = InterruptCoordinator()
    coord._should_pause = True  # pause already requested (signal_pause fired)
    # A retryable network error — would normally return a backoff delay.
    runner = _Runner([{"type": "error", "payload": {
        "category": "network", "kind": "no_result_message",
        "message": "SDK closed without yielding a ResultMessage"}}])
    res = await drive_invocation(_inv(tmp_path), runner,
                                 coordinator=coord, log=None, emit_events=False)
    assert res.paused is True
    assert res.delay is None          # NOT retried
    assert res.errored is False


@pytest.mark.anyio
async def test_drive_invocation_catches_subagent_suspended(tmp_path: Path) -> None:
    from imp.engine.agent_run import SubagentSuspended, drive_invocation

    coord = InterruptCoordinator()

    class _SuspendingRunner:
        async def run(self, invocation):
            raise SubagentSuspended("nested pause")
            yield  # pragma: no cover (makes this an async generator)
        async def interrupt(self) -> None: ...

    res = await drive_invocation(_inv(tmp_path), _SuspendingRunner(),
                                 coordinator=coord, log=None, emit_events=False)
    assert res.paused is True


@pytest.mark.anyio
async def test_drive_invocation_emit_events_appends_to_log(tmp_path: Path) -> None:
    from imp.engine.agent_run import drive_invocation

    class _Log:
        def __init__(self):
            self.calls: list[dict] = []
        def append(self, *, agent_id, type, payload):
            self.calls.append({"agent_id": agent_id, "type": type, "payload": payload})

    coord = InterruptCoordinator()
    log = _Log()
    runner = _Runner([
        {"type": "thinking", "payload": {"x": 1}},
        {"type": "agent_output", "payload": {"status": "Success"}},
    ])
    inv = _inv(tmp_path)
    res = await drive_invocation(inv, runner, coordinator=coord, log=log, emit_events=True)

    assert res.output == {"status": "Success"}
    assert res.errored is False
    # Both drained events must have been appended with the invocation's agent_id.
    assert any(
        c["agent_id"] == "step:p/s" and c["type"] == "thinking"
        for c in log.calls
    ), f"thinking event not found in log.calls: {log.calls}"
    assert any(
        c["agent_id"] == "step:p/s" and c["type"] == "agent_output"
        for c in log.calls
    ), f"agent_output event not found in log.calls: {log.calls}"


@pytest.mark.anyio
async def test_drive_invocation_output_validation_failure(tmp_path: Path) -> None:
    # {"status": "Nope"} fails because "Nope" is not in the enum values
    # ("Success", "Error"), so validate_agent_output raises LoadError.
    from imp.engine.agent_run import drive_invocation

    class _Log:
        def __init__(self):
            self.calls: list[dict] = []
        def append(self, *, agent_id, type, payload):
            self.calls.append({"agent_id": agent_id, "type": type, "payload": payload})

    coord = InterruptCoordinator()
    log = _Log()
    runner = _Runner([{"type": "agent_output", "payload": {"status": "Nope"}}])
    inv = _inv(tmp_path)
    res = await drive_invocation(inv, runner, coordinator=coord, log=log, emit_events=True)

    assert res.errored is True
    assert res.output is None
    assert res.error is not None
    assert res.error.category == "agent_output_missing"
    assert res.error.kind == "output_validation"
    # A synthesized error event must have been appended with agent_id=None.
    error_events = [c for c in log.calls if c["type"] == "error" and c["agent_id"] is None]
    assert error_events, f"no synthesized error event found in log.calls: {log.calls}"
    assert error_events[0]["payload"]["category"] == "agent_output_missing"


@pytest.mark.anyio
async def test_drive_invocation_stalls_on_inactivity(
    tmp_path: Path, monkeypatch,
) -> None:
    """B.2: a runner that emits NOTHING past the inactivity window is killed; the
    runner's own interrupt() runs and the result is errored with kind 'stalled'."""
    import imp.engine.agent_run as ar

    monkeypatch.setattr(ar, "AGENT_INACTIVITY_TIMEOUT_S", 0.2)

    interrupted = {"called": False}

    class _StallRunner:
        async def interrupt(self) -> None:
            interrupted["called"] = True

        async def run(self, _invocation):
            await anyio.sleep(3600)   # emit nothing, ever
            yield {"type": "agent_output", "payload": {}}  # never reached

    with anyio.fail_after(5):
        result = await ar.drive_invocation(
            _inv(tmp_path), _StallRunner(), coordinator=None, log=None,
            emit_events=False)
    assert result.errored is True
    assert result.error is not None
    assert result.error.category == "stalled"   # not "config"
    assert result.error.kind == "stalled"
    # Routed through the policy: a non-None backoff delay drives the executor's
    # budgeted-backoff path. prior_backoff_attempts defaults to 0 -> 1.0s.
    assert result.delay == 1.0
    assert interrupted["called"] is True


@pytest.mark.anyio
async def test_drive_invocation_stall_logs_stalled_category(
    tmp_path: Path, monkeypatch,
) -> None:
    """D2: the watchdog stall logs an `error` event with category 'stalled'
    (not 'config') and returns a RetryWithBackoff result, so the executor's
    self-healed-retry path handles it."""
    import imp.engine.agent_run as ar

    monkeypatch.setattr(ar, "AGENT_INACTIVITY_TIMEOUT_S", 0.2)

    class _Log:
        def __init__(self):
            self.calls: list[dict] = []

        def append(self, *, agent_id, type, payload):
            self.calls.append({"agent_id": agent_id, "type": type, "payload": payload})
            return {"id": "e"}

    class _StallRunner:
        async def interrupt(self) -> None:
            pass

        async def run(self, _invocation):
            await anyio.sleep(3600)
            yield {"type": "agent_output", "payload": {}}  # never reached

    log = _Log()
    with anyio.fail_after(5):
        result = await ar.drive_invocation(
            _inv(tmp_path), _StallRunner(), coordinator=None, log=log,
            emit_events=True, prior_backoff_attempts=2)

    errors = [c for c in log.calls if c["type"] == "error"]
    assert errors, f"no error event logged: {log.calls}"
    assert errors[0]["payload"]["category"] == "stalled"
    assert errors[0]["payload"]["kind"] == "stalled"
    # prior_backoff_attempts=2 -> schedule term 4.0s, plumbed onto the result.
    assert result.error is not None and result.error.category == "stalled"
    assert result.delay == 4.0


@pytest.mark.anyio
async def test_watchdog_teardown_force_kills_when_interrupt_ignores_grace(
    tmp_path: Path, monkeypatch,
) -> None:
    """AC6 bounded teardown: a wedged runner is force-killed within PROCESS_KILL_GRACE_S."""
    import imp.engine.agent_run as ar

    monkeypatch.setattr(ar, "AGENT_INACTIVITY_TIMEOUT_S", 0.2)
    monkeypatch.setattr(ar, "PROCESS_KILL_GRACE_S", 0.3)

    forced = {"called": False}

    class _IgnoresInterruptRunner:
        async def interrupt(self) -> None:
            await anyio.sleep(3600)   # ignores the graceful interrupt forever

        async def force_kill(self) -> None:
            forced["called"] = True

        async def run(self, _invocation):
            await anyio.sleep(3600)   # emit nothing, ever
            yield {"type": "agent_output", "payload": {}}  # never reached

    start = anyio.current_time()
    with anyio.fail_after(5):
        result = await ar.drive_invocation(
            _inv(tmp_path), _IgnoresInterruptRunner(), coordinator=None,
            log=None, emit_events=False)
    elapsed = anyio.current_time() - start

    assert forced["called"] is True
    assert result.errored is True
    assert result.error is not None and result.error.category == "stalled"
    assert result.delay == 1.0
    # Bounded: ~AGENT_INACTIVITY_TIMEOUT_S + ~PROCESS_KILL_GRACE_S, NOT ~3600s.
    # Generous ceiling for CI jitter, far below the hang it replaces.
    assert elapsed < 2.0


@pytest.mark.anyio
async def test_drive_invocation_survives_when_subtree_active(
    tmp_path: Path, monkeypatch,
) -> None:
    """B.2 false-positive fix: an agent whose OWN generator yields nothing for
    far longer than the inactivity window is NOT torn down, as long as the shared
    subtree keeps signalling liveness via `coordinator.note_activity()`.

    This is the real claude_code shape: a task-lead parked in an in-process
    subagent dispatch (or a busy leaf that appends to the shared elog without
    yielding) produces no generator yields for minutes, yet is fully alive.

    The agent stays silent for ~4x the window while activity is signalled every
    ~half-window, then finally yields its output. A correct fix delivers that
    output. A timestamp-only non-fix that kept `with fail_after(window): await
    gen.__anext__()` would still fire at start+window (the single bounded await
    never returns), so this test also guards against that vacuous change.
    """
    import imp.engine.agent_run as ar

    monkeypatch.setattr(ar, "AGENT_INACTIVITY_TIMEOUT_S", 0.2)
    coord = InterruptCoordinator()
    interrupted = {"called": False}

    class _BusyButSilentRunner:
        async def interrupt(self) -> None:
            interrupted["called"] = True

        async def run(self, _invocation):
            # Yield NOTHING for ~0.8s (4x the 0.2s window) while the subtree
            # stays active — note_activity every 0.1s (< window) must keep the
            # watchdog from tripping.
            for _ in range(8):
                await anyio.sleep(0.1)
                coord.note_activity()
            yield {"type": "agent_output", "payload": {"status": "Success"}}

    with anyio.fail_after(5):
        result = await ar.drive_invocation(
            _inv(tmp_path), _BusyButSilentRunner(), coordinator=coord,
            log=None, emit_events=False)

    assert result.errored is False, "busy-but-silent agent wrongly torn down"
    assert result.output == {"status": "Success"}
    assert interrupted["called"] is False


@pytest.mark.anyio
async def test_drive_invocation_stalls_with_coordinator_but_no_activity(
    tmp_path: Path, monkeypatch,
) -> None:
    """True-positive guard WITH a real coordinator present: a genuinely hung
    agent that emits nothing AND signals no subtree activity is still torn down
    as config/stalled. Confirms the liveness wiring doesn't keep a silent agent
    alive merely by being present (the guard the plan requires alongside the
    busy-survives case)."""
    import imp.engine.agent_run as ar

    monkeypatch.setattr(ar, "AGENT_INACTIVITY_TIMEOUT_S", 0.2)
    coord = InterruptCoordinator()
    interrupted = {"called": False}

    class _StallRunner:
        async def interrupt(self) -> None:
            interrupted["called"] = True

        async def run(self, _invocation):
            await anyio.sleep(3600)   # no events, no note_activity, ever
            yield {"type": "agent_output", "payload": {}}  # never reached

    with anyio.fail_after(5):
        result = await ar.drive_invocation(
            _inv(tmp_path), _StallRunner(), coordinator=coord, log=None,
            emit_events=False)
    assert result.errored is True
    assert result.error is not None and result.error.kind == "stalled"
    assert interrupted["called"] is True


def test_shared_log_append_pulses_coordinator_activity(tmp_path: Path) -> None:
    """B.2 production wiring: every append to the run's shared event log pulses
    coordinator.note_activity() (which resets the inactivity watchdog), and the
    original on_event fan-out is preserved. Without this, the unit-level watchdog
    fix would pass while the real claude_code window never resets — the live
    hazard the plan calls out."""
    from imp.engine.executor import _liveness_on_event
    from imp.engine.state import JsonlEventLog

    coord = InterruptCoordinator()
    pulses = {"n": 0}
    coord.add_activity_listener(lambda: pulses.__setitem__("n", pulses["n"] + 1))
    seen: list[dict] = []

    log = JsonlEventLog(
        tmp_path / "e.jsonl", on_event=_liveness_on_event(coord, seen.append))
    log.append(agent_id="step:p/s", type="thinking", payload={"x": 1})

    assert pulses["n"] == 1, "append did not pulse coordinator.note_activity()"
    assert len(seen) == 1, "composed on_event dropped the original driver callback"


def _build_inv(tmp_path: Path, **kw):
    from imp.engine.agent_run import build_agent_invocation
    return build_agent_invocation(
        agent_def=_agent(tools=frozenset({"bash", "complete_step"})),
        provider_name="anthropic", model_name="m",
        rendered_system_prompt="BODY", initial_messages=(),
        change_context=_cc(tmp_path), user_chat_input_fn=None,
        transcript_dir=tmp_path,events_path=tmp_path / "e",
        subagent_defs={}, tiers=(), max_tool_calls=None, agent_id="step:p/s",
        event_log=None, change_state=None, first_event_parent_id=None,
        resolved_tiers=None, coordinator=None, **kw,
    )


def test_build_agent_invocation_splits_role_header_and_user_task(tmp_path: Path) -> None:
    # Top-level: system prompt is role-only (no rendered body, no seed note, no
    # completion contract); the rendered body + completion contract move into a
    # single first user message.
    from imp.engine.agent_run import _CORE_HEAD, _CORE_TAIL, build_agent_invocation

    inv = build_agent_invocation(
        agent_def=_agent(tools={"complete_step", "bash"}),
        provider_name="anthropic", model_name="m",
        rendered_system_prompt="RENDERED BODY",
        initial_messages=(), change_context=_cc(tmp_path),
        user_chat_input_fn=None,
        transcript_dir=tmp_path / "td",
        events_path=tmp_path / "e.jsonl", subagent_defs={}, tiers=(),
        max_tool_calls=None, agent_id="step:p/s", event_log=None,
        change_state=None, first_event_parent_id=None,
        resolved_tiers=None, coordinator=None)

    sp = inv.system_prompt
    assert sp.startswith(reflow_markdown(_CORE_HEAD))
    assert reflow_markdown(_CORE_TAIL) in sp
    assert "IMP_PROJECT_ROOT" in sp                      # bash note stays in system
    assert "RENDERED BODY" not in sp                     # body left the system prompt
    assert "<imp:begin/>" not in sp                      # sentinel note deleted
    assert "complete_step" not in sp                     # completion contract left too

    assert len(inv.initial_messages) == 1
    user = inv.initial_messages[0]
    assert user.role == "user"
    text = user.content[0].text
    assert "RENDERED BODY" in text
    assert text.index("RENDERED BODY") < text.index("complete_step")


def test_build_invocation_override_is_used_verbatim(tmp_path: Path) -> None:
    inv = _build_inv(tmp_path, system_prompt_override="EXACT STORED PROMPT")
    assert inv.system_prompt == "EXACT STORED PROMPT"        # no notes appended


def test_build_agent_invocation_appends_current_date_line(
    tmp_path: Path, monkeypatch,
) -> None:
    """The non-resume assembly appends exactly one `Today's date is <YYYY-MM-DD>.`
    line, computed at invocation-build time. Pin the date by subclassing
    datetime.date so the assertion is deterministic and falsifiable (it fails if
    the line is absent, misworded, or wrongly formatted)."""
    import datetime as _dt

    import imp.engine.agent_run as ar

    class _FixedDate(_dt.date):
        @classmethod
        def today(cls) -> _dt.date:
            return _dt.date(2026, 6, 29)

    monkeypatch.setattr(ar, "date", _FixedDate)
    inv = _build_inv(tmp_path)
    assert "Today's date is 2026-06-29." in inv.system_prompt


def test_build_agent_invocation_omits_current_date_line_on_resume_override(
    tmp_path: Path,
) -> None:
    """The resume-replay branch reuses the stored prompt verbatim — no date line
    is appended, so a resumed step keeps its original frozen date."""
    inv = _build_inv(tmp_path, system_prompt_override="EXACT STORED PROMPT")
    assert inv.system_prompt == "EXACT STORED PROMPT"
    assert "Today's date is" not in inv.system_prompt


def test_build_invocation_no_override_assembles_role_header(tmp_path: Path) -> None:
    from imp.engine.agent_run import _CORE_HEAD
    inv = _build_inv(tmp_path)
    assert inv.system_prompt.startswith(reflow_markdown(_CORE_HEAD))
    assert "BODY" not in inv.system_prompt                # body moved to the user turn
    assert "Session start" not in inv.system_prompt       # seed note deleted
    assert "BODY" in inv.initial_messages[0].content[0].text


def test_build_invocation_subagent_keeps_body_in_system(tmp_path: Path) -> None:
    from imp.engine.agent_run import SUBAGENT_DELIVERABLE_NOTE, build_agent_invocation
    task = (Message(role="user", content=(TextBlock(text="SUBAGENT TASK"),)),)
    inv = build_agent_invocation(
        agent_def=_agent(), provider_name="anthropic", model_name="m",
        rendered_system_prompt="RENDERED BODY",
        initial_messages=task, change_context=_cc(tmp_path),
        user_chat_input_fn=None, transcript_dir=tmp_path / "td",
        events_path=tmp_path / "e.jsonl", subagent_defs={}, tiers=(),
        max_tool_calls=None, agent_id="sub:x", event_log=None,
        change_state=None, first_event_parent_id=None,
        resolved_tiers=None, coordinator=None, subagent_depth=1)
    assert "RENDERED BODY" in inv.system_prompt
    assert SUBAGENT_DELIVERABLE_NOTE in inv.system_prompt
    assert "complete_step" not in inv.system_prompt
    assert inv.initial_messages == task


def test_build_invocation_prepends_feedback_lead_to_user_task(tmp_path: Path) -> None:
    from imp.engine.agent_run import build_agent_invocation
    inv = build_agent_invocation(
        agent_def=_agent(), provider_name="anthropic", model_name="m",
        rendered_system_prompt="RENDERED BODY",
        initial_messages=(), change_context=_cc(tmp_path),
        user_chat_input_fn=None, transcript_dir=tmp_path / "td",
        events_path=tmp_path / "e.jsonl", subagent_defs={}, tiers=(),
        max_tool_calls=None, agent_id="step:p/s", event_log=None,
        change_state=None, first_event_parent_id=None,
        resolved_tiers=None, coordinator=None,
        feedback_lead="Here's the feedback to address: fix X")
    text = inv.initial_messages[0].content[0].text
    assert text.startswith("Here's the feedback to address: fix X")
    assert text.index("Here's the feedback") < text.index("RENDERED BODY")
    assert text.index("RENDERED BODY") < text.index("complete_step")


@pytest.mark.anyio
async def test_drive_invocation_stalls_mid_stream(
    tmp_path: Path, monkeypatch,
) -> None:
    """F6: the runner emits SOME events, then goes silent past the window. The
    watchdog must still fire (exercises the wrapper after the loop body has run a
    few times)."""
    import imp.engine.agent_run as ar

    monkeypatch.setattr(ar, "AGENT_INACTIVITY_TIMEOUT_S", 0.2)
    interrupted = {"called": False}

    class _MidStallRunner:
        async def interrupt(self) -> None:
            interrupted["called"] = True

        async def run(self, _invocation):
            yield {"type": "thinking", "payload": {"content": "warming up"}}
            yield {"type": "thinking", "payload": {"content": "still going"}}
            await anyio.sleep(3600)        # now go silent forever
            yield {"type": "agent_output", "payload": {}}  # never reached

    with anyio.fail_after(5):
        result = await ar.drive_invocation(
            _inv(tmp_path), _MidStallRunner(), coordinator=None, log=None,
            emit_events=False)
    assert result.errored is True
    assert result.error is not None and result.error.kind == "stalled"
    assert interrupted["called"] is True



@pytest.mark.anyio
async def test_pull_watchdog_frozen_during_solicit_does_not_stall(monkeypatch) -> None:
    """While solicit_active is True, a pull that blocks far past the window is
    NOT torn down: the deadline is frozen to inf. A single begin call with NO
    further activity, then a 4x-window sleep, distinguishes the inf-freeze from a
    one-shot now+window bump (which would expire at ~1 window and stall)."""
    import imp.engine.agent_run as ar

    monkeypatch.setattr(ar, "AGENT_INACTIVITY_TIMEOUT_S", 0.2)
    coord = InterruptCoordinator()
    interrupted = {"called": False}

    class _Runner:
        async def interrupt(self) -> None:
            interrupted["called"] = True

    async def _gen():
        coord.begin_solicit_watchdog()   # freeze (single call, no further activity)
        await anyio.sleep(0.8)           # 4x window, silent
        coord.end_solicit_watchdog()
        yield {"type": "agent_output", "payload": {"status": "Success"}}

    stall = {"hit": False}
    events = []
    with anyio.fail_after(5):
        async for ev in ar._pull_with_inactivity_watchdog(
                _gen(), runner=_Runner(), stall=stall, coordinator=coord):
            events.append(ev)

    assert stall["hit"] is False
    assert interrupted["called"] is False
    assert events == [{"type": "agent_output", "payload": {"status": "Success"}}]


@pytest.mark.anyio
async def test_pull_watchdog_stalls_when_not_soliciting(monkeypatch) -> None:
    """Genuine stalls are unaffected: with solicit_active False, an agent silent
    past the window (no activity) is still torn down as a stall."""
    import imp.engine.agent_run as ar

    monkeypatch.setattr(ar, "AGENT_INACTIVITY_TIMEOUT_S", 0.2)
    coord = InterruptCoordinator()
    interrupted = {"called": False}

    class _Runner:
        async def interrupt(self) -> None:
            interrupted["called"] = True

    async def _gen():
        await anyio.sleep(3600)          # never solicits, never signals activity
        yield {"type": "agent_output", "payload": {}}  # unreached

    stall = {"hit": False}
    events = []
    with anyio.fail_after(5):
        async for ev in ar._pull_with_inactivity_watchdog(
                _gen(), runner=_Runner(), stall=stall, coordinator=coord):
            events.append(ev)

    assert stall["hit"] is True
    assert interrupted["called"] is True
    assert events == []


@pytest.mark.anyio
async def test_pull_watchdog_restores_finite_window_after_end(monkeypatch) -> None:
    """end_solicit_watchdog restores an ENFORCEABLE finite deadline: after a
    frozen interval ends, a subsequent over-window silence stalls again. Guards
    against end leaving the deadline frozen at inf forever."""
    import imp.engine.agent_run as ar

    monkeypatch.setattr(ar, "AGENT_INACTIVITY_TIMEOUT_S", 0.2)
    coord = InterruptCoordinator()
    interrupted = {"called": False}

    class _Runner:
        async def interrupt(self) -> None:
            interrupted["called"] = True

    async def _gen():
        coord.begin_solicit_watchdog()
        await anyio.sleep(0.4)           # frozen: survives 2x window
        coord.end_solicit_watchdog()     # restore finite window
        await anyio.sleep(3600)          # now silent past the window -> must stall
        yield {"type": "agent_output", "payload": {}}  # unreached

    stall = {"hit": False}
    events = []
    with anyio.fail_after(5):
        async for ev in ar._pull_with_inactivity_watchdog(
                _gen(), runner=_Runner(), stall=stall, coordinator=coord):
            events.append(ev)

    assert stall["hit"] is True
    assert interrupted["called"] is True
    assert events == []


class _RecordingCoord:
    """Records begin/end_solicit_watchdog and the active flag; drains no
    interjects (so _resolve_wait_reply falls through to soliciting)."""

    def __init__(self):
        self.events = []
        self.active = False

    def drain_interject(self):
        return None

    def begin_solicit_watchdog(self):
        self.active = True
        self.events.append("begin")

    def end_solicit_watchdog(self):
        self.active = False
        self.events.append("end")


@pytest.mark.anyio
async def test_resolve_wait_reply_brackets_await_with_begin_end() -> None:
    import imp.engine.agent_run as ar
    from imp.engine.state import InMemoryEventLog
    coord = _RecordingCoord()
    log = InMemoryEventLog()

    async def _ainput(_q):
        assert coord.active is True   # watchdog frozen DURING the await
        return "resumed reply"

    reply, source = await ar._resolve_wait_reply(
        coord, _ainput, elog=log, agent_id="step:p/s")
    assert (reply, source) == ("resumed reply", "solicited")
    assert coord.events == ["begin", "end"]
    assert coord.active is False
    # Genuine solicit emits the gate exactly once.
    assert [e["type"] for e in log.read()].count("awaiting_user_input") == 1


@pytest.mark.anyio
async def test_resolve_wait_reply_restores_watchdog_on_exception() -> None:
    import imp.engine.agent_run as ar
    from imp.engine.state import InMemoryEventLog
    coord = _RecordingCoord()
    log = InMemoryEventLog()

    async def _ainput(_q):
        raise RuntimeError("prompt torn down")

    with pytest.raises(RuntimeError):
        await ar._resolve_wait_reply(coord, _ainput, elog=log, agent_id="step:p/s")
    assert coord.events == ["begin", "end"]   # finally restored it on the exception
    assert coord.active is False
    assert [e["type"] for e in log.read()].count("awaiting_user_input") == 1


@pytest.mark.anyio
async def test_real_bare_turn_end_solicit_is_not_torn_down_by_watchdog(
    tmp_path, monkeypatch,
) -> None:
    """Regression for the live ~35-min loop: an attended agent blocked at a
    bare-turn-end solicit for far longer than the inactivity window is NOT killed
    by the watchdog. Once the human replies the turn continues. Drives the real
    drive_invocation -> _pull_with_inactivity_watchdog -> build_continuation_fn
    solicit bracket -> coordinator freeze, deterministically (no LLM)."""
    import imp.engine.agent_run as ar
    from imp.engine.completion import build_continuation_fn

    monkeypatch.setattr(ar, "AGENT_INACTIVITY_TIMEOUT_S", 0.2)
    coord = InterruptCoordinator()
    release = anyio.Event()
    interrupted = {"called": False}
    captured = {"reply": None}

    class _CaptureLog:
        def __init__(self):
            self.events = []

        def append(self, *, agent_id, type, payload):
            self.events.append({"type": type, "payload": payload})
            return {"id": "e"}

    log = _CaptureLog()

    async def _blocking_input(_q):
        await release.wait()          # block until the releaser fires (past window)
        return "ship it"

    class _SolicitRunner:
        async def interrupt(self) -> None:
            interrupted["called"] = True

        async def run(self, invocation):
            cont = build_continuation_fn(
                user_input_fn=_blocking_input, coordinator=coord,
                event_log=log, agent_id="step:p/s")
            captured["reply"] = await cont("merge?")
            yield {"type": "agent_output", "payload": {"status": "Success"}}

    result_holder = {}

    async def _releaser():
        # Stay silent (no reply) for 3x the inactivity window: a pre-fix watchdog
        # tears the solicit down here at ~1 window.
        await anyio.sleep(0.6)
        release.set()

    async def _drive():
        result_holder["r"] = await ar.drive_invocation(
            _inv(tmp_path), _SolicitRunner(), coordinator=coord,
            log=log, emit_events=True)

    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(_releaser)
            tg.start_soon(_drive)

    result = result_holder["r"]
    assert result.errored is False
    assert result.output == {"status": "Success"}
    assert captured["reply"] == "ship it"          # turn continued after the reply
    assert interrupted["called"] is False          # solicit never torn down
    stalls = [
        e for e in log.events
        if e["type"] == "error"
        and e["payload"].get("category") == "stalled"
        and e["payload"].get("kind") == "stalled"
    ]
    assert stalls == []                            # no false config/stalled teardown


def test_build_agent_invocation_injects_outcome_field_when_requested(
    tmp_path: Path,
) -> None:
    from imp.engine.agent_run import build_agent_invocation

    inv = build_agent_invocation(
        agent_def=_agent(), provider_name="anthropic", model_name="m",
        rendered_system_prompt="BODY", initial_messages=(),
        change_context=_cc(tmp_path),
        user_chat_input_fn=None, transcript_dir=tmp_path / "td",
events_path=tmp_path / "e.jsonl",
        subagent_defs={}, tiers=(), max_tool_calls=None,
        agent_id="step:p/s", event_log=None, change_state=None,
        first_event_parent_id=None, resolved_tiers=None, coordinator=None,
        inject_outcome_field=True)

    fields = inv.agent_def.output_schema.fields
    outcome = next(f for f in fields if f.name == "outcome")
    assert outcome.required is True
    assert outcome.type == "string"
    assert outcome.description != ""  # model-facing guidance is present


def test_build_agent_invocation_omits_outcome_field_by_default(
    tmp_path: Path,
) -> None:
    from imp.engine.agent_run import build_agent_invocation

    inv = build_agent_invocation(
        agent_def=_agent(), provider_name="anthropic", model_name="m",
        rendered_system_prompt="BODY", initial_messages=(),
        change_context=_cc(tmp_path),
        user_chat_input_fn=None, transcript_dir=tmp_path / "td",
events_path=tmp_path / "e.jsonl",
        subagent_defs={}, tiers=(), max_tool_calls=None,
        agent_id="step:p/s", event_log=None, change_state=None,
        first_event_parent_id=None, resolved_tiers=None, coordinator=None)

    assert "outcome" not in [f.name for f in inv.agent_def.output_schema.fields]


def test_with_outcome_field_is_idempotent() -> None:
    from imp.engine.agent_run import with_outcome_field

    once = with_outcome_field(_agent())
    twice = with_outcome_field(once)
    names = [f.name for f in twice.output_schema.fields]
    assert names.count("outcome") == 1


@pytest.mark.anyio
async def test_pull_watchdog_frozen_during_tool_does_not_stall(monkeypatch) -> None:
    """While tool_active is True, a pull that blocks far past the window is NOT
    torn down: the deadline is frozen to inf. A single begin call with NO further
    activity, then a 4x-window sleep, distinguishes the inf-freeze from a one-shot
    now+window bump (which would expire at ~1 window and stall)."""
    import imp.engine.agent_run as ar

    monkeypatch.setattr(ar, "AGENT_INACTIVITY_TIMEOUT_S", 0.2)
    coord = InterruptCoordinator()
    interrupted = {"called": False}

    class _Runner:
        async def interrupt(self) -> None:
            interrupted["called"] = True

    async def _gen():
        coord.begin_tool_watchdog()   # freeze (single call, no further activity)
        await anyio.sleep(0.8)        # 4x window, silent
        coord.end_tool_watchdog()
        yield {"type": "agent_output", "payload": {"status": "Success"}}

    stall = {"hit": False}
    events = []
    with anyio.fail_after(5):
        async for ev in ar._pull_with_inactivity_watchdog(
                _gen(), runner=_Runner(), stall=stall, coordinator=coord):
            events.append(ev)

    assert stall["hit"] is False
    assert interrupted["called"] is False
    assert events == [{"type": "agent_output", "payload": {"status": "Success"}}]


@pytest.mark.anyio
async def test_pull_watchdog_restores_finite_window_after_tool_end(monkeypatch) -> None:
    """end_tool_watchdog (last tool returns) restores an ENFORCEABLE finite
    deadline: after the frozen interval ends, a subsequent over-window silence
    stalls again. Guards against the tool freeze leaving the deadline at inf."""
    import imp.engine.agent_run as ar

    monkeypatch.setattr(ar, "AGENT_INACTIVITY_TIMEOUT_S", 0.2)
    coord = InterruptCoordinator()
    interrupted = {"called": False}

    class _Runner:
        async def interrupt(self) -> None:
            interrupted["called"] = True

    async def _gen():
        coord.begin_tool_watchdog()
        await anyio.sleep(0.4)        # frozen: survives 2x window
        coord.end_tool_watchdog()     # last tool returns -> finite window restored
        await anyio.sleep(3600)       # now silent past the window -> must stall
        yield {"type": "agent_output", "payload": {}}  # unreached

    stall = {"hit": False}
    events = []
    with anyio.fail_after(5):
        async for ev in ar._pull_with_inactivity_watchdog(
                _gen(), runner=_Runner(), stall=stall, coordinator=coord):
            events.append(ev)

    assert stall["hit"] is True
    assert interrupted["called"] is True
    assert events == []


@pytest.mark.anyio
async def test_bump_leaves_deadline_untouched_once_cancel_called(monkeypatch) -> None:
    """The _bump activity listener must NOT mutate the scope deadline once the
    scope's cancellation is already in flight (cancel_called True). When
    cancel_called is False it still resets to a finite window, or freezes to inf
    under solicit_active/tool_active. Captures the listener via a fake
    coordinator and drives it with fake scopes — no real anyio timing."""
    import math

    import imp.engine.agent_run as ar

    monkeypatch.setattr(ar, "AGENT_INACTIVITY_TIMEOUT_S", 100.0)

    class _CaptureCoord:
        def __init__(self) -> None:
            self.listener = None
            self.solicit_active = False
            self.tool_active = False

        def add_activity_listener(self, fn):
            self.listener = fn
            return fn

        def remove_activity_listener(self, fn) -> None:
            if self.listener is fn:
                self.listener = None

        def note_activity(self) -> None:
            pass

    class _FakeScope:
        def __init__(self, cancel_called: bool) -> None:
            self.deadline = 555.0
            self.cancel_called = cancel_called

    coord = _CaptureCoord()
    results: dict[str, float] = {}

    async def _gen():
        bump = coord.listener
        assert bump is not None, "watchdog did not register the _bump listener"

        # cancel_called True -> deadline left untouched (the root-cause guard).
        s1 = _FakeScope(cancel_called=True)
        coord.solicit_active = False
        coord.tool_active = False
        bump(s1)
        results["cancelled"] = s1.deadline

        # cancel_called False, no freeze -> finite now+window reset.
        s2 = _FakeScope(cancel_called=False)
        bump(s2)
        results["finite"] = s2.deadline

        # cancel_called False, solicit_active -> frozen to inf.
        s3 = _FakeScope(cancel_called=False)
        coord.solicit_active = True
        bump(s3)
        results["solicit"] = s3.deadline

        # cancel_called False, tool_active -> frozen to inf.
        s4 = _FakeScope(cancel_called=False)
        coord.solicit_active = False
        coord.tool_active = True
        bump(s4)
        results["tool"] = s4.deadline

        yield {"type": "agent_output", "payload": {"status": "Success"}}

    stall = {"hit": False}
    events = []
    with anyio.fail_after(5):
        async for ev in ar._pull_with_inactivity_watchdog(
                _gen(), runner=object(), stall=stall, coordinator=coord):
            events.append(ev)

    assert results["cancelled"] == 555.0            # untouched once cancel in flight
    assert results["finite"] != math.inf            # reset, not frozen
    assert results["finite"] == pytest.approx(anyio.current_time() + 100.0, abs=1.0)
    assert results["solicit"] == math.inf
    assert results["tool"] == math.inf
    assert stall["hit"] is False                    # happy path is not a stall
    assert events == [{"type": "agent_output", "payload": {"status": "Success"}}]


@pytest.mark.anyio
async def test_pull_watchdog_treats_swallowed_cancel_fallthrough_as_stall(
        monkeypatch) -> None:
    """Simulates the swallowed-self-cancel fall-through: a fake fail_after scope
    that suppresses the pull's exception leaves ev unset without raising, so the
    safety-net branch routes through _teardown_stalled_agent instead of yielding
    an unbound ev."""
    import imp.engine.agent_run as ar

    interrupted = {"called": False}

    class _Runner:
        async def interrupt(self) -> None:
            interrupted["called"] = True

    class _FakeScope:
        deadline = 0.0
        cancel_called = False

    class _FakeCM:
        def __enter__(self):
            return _FakeScope()

        def __exit__(self, exc_type, exc, tb):
            return exc_type is not None  # swallow all exceptions, leaving ev unset

    monkeypatch.setattr(ar.anyio, "fail_after", lambda _: _FakeCM())

    async def _gen():
        raise RuntimeError("boom")
        yield  # unreachable; required to make this an async generator

    stall = {"hit": False}
    events = []
    async for ev in ar._pull_with_inactivity_watchdog(
            _gen(), runner=_Runner(), stall=stall, coordinator=None):
        events.append(ev)

    assert events == []
    assert stall["hit"] is True
    assert interrupted["called"] is True


@pytest.mark.anyio
async def test_inline_solicit_raises_awaiting_when_no_channel() -> None:
    from imp.engine.agent_run import build_inline_solicit
    from imp.engine.awaiting_user_input import AwaitingUserInput

    fn = build_inline_solicit(user_input_fn=None)
    with pytest.raises(AwaitingUserInput):
        await fn("the agent's last words")


@pytest.mark.anyio
async def test_inline_solicit_returns_reply_and_emits_awaiting() -> None:
    from imp.engine.agent_run import build_inline_solicit
    from imp.engine.state import InMemoryEventLog

    log = InMemoryEventLog()
    fn = build_inline_solicit(
        user_input_fn=lambda _prompt: "redirect now",
        event_log=log, agent_id="step:p/s")

    reply = await fn("the agent's last words")

    assert reply == "redirect now"
    assert any(e["type"] == "awaiting_user_input" for e in log.read())


@pytest.mark.anyio
async def test_drive_invocation_maps_handoff_event_to_result(tmp_path):
    from imp.agents.runner import AgentInvocation, ChangeContext
    from imp.engine.agent_run import drive_invocation
    from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc

    class _HandoffRunner:
        async def run(self, inv):
            yield {"type": "handoff",
                   "payload": {"handoff_path": "/x/s.handoff.md"}}

        async def interrupt(self):
            ...

    agent_def = AgentDef(
        name="a", description="", tools=frozenset(), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1)),
        prompt_template_source="go", source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1))
    inv = AgentInvocation(
        agent_def=agent_def, provider_name="fake", model_name="m",
        system_prompt="p", initial_messages=(),
        change_context=ChangeContext(title="t", project_root=tmp_path,
                                     workflow_dir=tmp_path),
        transcript_dir=tmp_path / "s", events_path=tmp_path / "e.jsonl")

    result = await drive_invocation(
        inv, _HandoffRunner(), coordinator=None, log=None, emit_events=False)

    assert result.handoff_path == "/x/s.handoff.md"
    assert result.errored is False
    assert result.fatal is False
    assert result.paused is False
    assert result.output is None


@pytest.mark.anyio
async def test_drive_invocation_handoff_event_not_logged(tmp_path):
    from imp.agents.runner import AgentInvocation, ChangeContext
    from imp.engine.agent_run import drive_invocation
    from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc

    class _Log:
        def __init__(self):
            self.calls: list[dict] = []
        def append(self, *, agent_id, type, payload):
            self.calls.append({"agent_id": agent_id, "type": type, "payload": payload})

    class _HandoffRunner:
        async def run(self, inv):
            yield {"type": "handoff",
                   "payload": {"handoff_path": "/x/s.handoff.md"}}

        async def interrupt(self):
            ...

    agent_def = AgentDef(
        name="a", description="", tools=frozenset(), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1)),
        prompt_template_source="go", source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1))
    inv = AgentInvocation(
        agent_def=agent_def, provider_name="fake", model_name="m",
        system_prompt="p", initial_messages=(),
        change_context=ChangeContext(title="t", project_root=tmp_path,
                                     workflow_dir=tmp_path),
        transcript_dir=tmp_path / "s", events_path=tmp_path / "e.jsonl")

    log = _Log()
    result = await drive_invocation(
        inv, _HandoffRunner(), coordinator=None, log=log, emit_events=True)

    assert result.handoff_path == "/x/s.handoff.md"
    assert log.calls == []


def test_build_agent_invocation_threads_resume_should_continue(
    tmp_path: Path,
) -> None:
    from imp.engine.agent_run import build_agent_invocation

    inv = build_agent_invocation(
        agent_def=_agent(), provider_name="anthropic", model_name="m",
        rendered_system_prompt="BODY", initial_messages=(),
        change_context=_cc(tmp_path), user_chat_input_fn=None,
        transcript_dir=tmp_path / "td",
        events_path=tmp_path / "e.jsonl", subagent_defs={}, tiers=(),
        max_tool_calls=None, agent_id="step:p/s", event_log=None,
        change_state=None, first_event_parent_id=None,
        resolved_tiers=None, coordinator=None,
        resume_should_continue=True)

    assert inv.resume_should_continue is True


def test_build_agent_invocation_resume_should_continue_defaults_false(
    tmp_path: Path,
) -> None:
    from imp.engine.agent_run import build_agent_invocation

    inv = build_agent_invocation(
        agent_def=_agent(), provider_name="anthropic", model_name="m",
        rendered_system_prompt="BODY", initial_messages=(),
        change_context=_cc(tmp_path), user_chat_input_fn=None,
        transcript_dir=tmp_path / "td",
        events_path=tmp_path / "e.jsonl", subagent_defs={}, tiers=(),
        max_tool_calls=None, agent_id="step:p/s", event_log=None,
        change_state=None, first_event_parent_id=None,
        resolved_tiers=None, coordinator=None)

    assert inv.resume_should_continue is False


class _OneInterjectCoord:
    """Drains exactly one queued interject, then empties. No watchdog needed:
    the interject branch returns before any solicit."""

    def __init__(self, text):
        self._queue = [text]

    def drain_interject(self):
        return self._queue.pop(0) if self._queue else None


@pytest.mark.anyio
async def test_resolve_wait_reply_interject_wins_over_interrupt_flag() -> None:
    import imp.engine.agent_run as ar
    from imp.engine.state import InMemoryEventLog
    log = InMemoryEventLog()

    async def _ainput(_q):
        raise AssertionError("must not solicit when an interject is queued")

    reply, source = await ar._resolve_wait_reply(
        _OneInterjectCoord("human says go"), _ainput, True,
        elog=log, agent_id="step:p/s")
    assert (reply, source) == ("human says go", "interject")
    # An interject is a human turn, not a solicit: no gate emit.
    assert "awaiting_user_input" not in [e["type"] for e in log.read()]


@pytest.mark.anyio
async def test_resolve_wait_reply_interrupt_flag_returns_continuation_nudge() -> None:
    import imp.engine.agent_run as ar
    from imp.engine.state import InMemoryEventLog
    coord = _RecordingCoord()  # drains no interjects
    log = InMemoryEventLog()

    async def _ainput(_q):
        raise AssertionError("must not solicit when resume_should_continue")

    reply, source = await ar._resolve_wait_reply(
        coord, _ainput, True, elog=log, agent_id="step:p/s")
    assert reply == ar._CONTINUATION_NUDGE
    assert source == "nudge"
    assert "complete_step" in reply
    assert coord.events == []  # never entered the solicit watchdog bracket
    # Retry-continue self-continues: no phantom prompt to the client.
    assert "awaiting_user_input" not in [e["type"] for e in log.read()]


@pytest.mark.anyio
async def test_resolve_wait_reply_no_flag_solicits_empty() -> None:
    import imp.engine.agent_run as ar
    from imp.engine.state import InMemoryEventLog
    coord = _RecordingCoord()
    log = InMemoryEventLog()
    seen = {}

    async def _ainput(q):
        seen["prompt"] = q
        return ""

    reply, source = await ar._resolve_wait_reply(
        coord, _ainput, False, elog=log, agent_id="step:p/s")
    assert (reply, source) == ("", "solicited")
    assert seen["prompt"] == ""        # solicited with the empty prompt
    assert coord.events == ["begin", "end"]  # the solicit watchdog bracket ran
    # The gate emit fires exactly once, right before soliciting.
    assert [e["type"] for e in log.read()].count("awaiting_user_input") == 1


def _capture_continuation(monkeypatch) -> dict:
    import imp.engine.agent_run as ar
    captured: dict = {}

    def _fake_build_continuation_fn(**kw):
        captured.update(kw)
        return lambda t: t

    monkeypatch.setattr(ar, "build_continuation_fn", _fake_build_continuation_fn)
    return captured


def _fieldless_agent() -> AgentDef:
    return AgentDef(
        name="x", description="", tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(), source=SourceLoc(file=Path("a.md"), line=1)),
        prompt_template_source="body", source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1), max_tool_calls=None)


def test_build_agent_invocation_threads_interactive(tmp_path: Path, monkeypatch) -> None:
    cap_true = _capture_continuation(monkeypatch)
    _build_inv(tmp_path, interactive=True)
    assert cap_true["interactive"] is True

    cap_default = _capture_continuation(monkeypatch)
    _build_inv(tmp_path)  # no interactive kwarg
    assert cap_default["interactive"] is False


def test_build_agent_invocation_derives_has_schema_true_when_fields(
    tmp_path: Path, monkeypatch,
) -> None:
    captured = _capture_continuation(monkeypatch)
    _build_inv(tmp_path)  # _agent() carries a non-empty output schema
    assert captured["has_schema"] is True


def test_build_agent_invocation_derives_has_schema_false_when_fieldless(
    tmp_path: Path, monkeypatch,
) -> None:
    from imp.engine.agent_run import build_agent_invocation
    captured = _capture_continuation(monkeypatch)
    build_agent_invocation(
        agent_def=_fieldless_agent(), provider_name="anthropic", model_name="m",
        rendered_system_prompt="BODY", initial_messages=(),
        change_context=_cc(tmp_path), user_chat_input_fn=None,
        transcript_dir=tmp_path, events_path=tmp_path / "e",
        subagent_defs={}, tiers=(), max_tool_calls=None, agent_id="step:p/s",
        event_log=None, change_state=None, first_event_parent_id=None,
        resolved_tiers=None, coordinator=None)
    assert captured["has_schema"] is False


def test_agent_run_result_delay_hinted_defaults_false() -> None:
    from imp.engine.agent_run import AgentRunResult
    res = AgentRunResult(output=None, errored=True, paused=False, fatal=False,
                         delay=7200.0)
    assert res.delay_hinted is False


@pytest.mark.anyio
async def test_drive_invocation_large_hint_sets_delay_hinted(tmp_path: Path) -> None:
    from imp.engine.agent_run import drive_invocation

    coord = InterruptCoordinator()
    runner = _Runner([{"type": "error", "payload": {
        "category": "rate_limit", "kind": "429", "message": "resets 4am",
        "detail": {"retry_after_seconds": 7200.0}}}])
    res = await drive_invocation(_inv(tmp_path), runner,
                                 coordinator=coord, log=None, emit_events=False,
                                 prior_backoff_attempts=0)
    assert res.delay == 7200.0
    assert res.delay_hinted is True


@pytest.mark.anyio
async def test_drive_invocation_small_hint_not_delay_hinted(tmp_path: Path) -> None:
    from imp.engine.agent_run import drive_invocation

    coord = InterruptCoordinator()
    runner = _Runner([{"type": "error", "payload": {
        "category": "rate_limit", "kind": "429", "message": "slow",
        "detail": {"retry_after_seconds": 30.0}}}])
    res = await drive_invocation(_inv(tmp_path), runner,
                                 coordinator=coord, log=None, emit_events=False,
                                 prior_backoff_attempts=0)
    assert res.delay == 30.0
    assert res.delay_hinted is False


@pytest.mark.anyio
async def test_drive_invocation_blind_backoff_not_delay_hinted(tmp_path: Path) -> None:
    from imp.engine.agent_run import drive_invocation

    coord = InterruptCoordinator()
    runner = _Runner([{"type": "error", "payload": {
        "category": "rate_limit", "kind": "429", "message": "slow"}}])
    res = await drive_invocation(_inv(tmp_path), runner,
                                 coordinator=coord, log=None, emit_events=False,
                                 prior_backoff_attempts=0)
    assert res.delay is not None
    assert res.delay_hinted is False

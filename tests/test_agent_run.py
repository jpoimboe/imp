from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from imp.agents.runner import ChangeContext
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
    return ChangeContext(slug="s", title="t", project_root=tmp,
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
    from imp.engine.agent_run import _AGENT_BASE_PROMPT, build_agent_invocation

    inv = build_agent_invocation(
        agent_def=_agent(), provider_name="anthropic", model_name="m",
        rendered_system_prompt="RENDERED BODY",
        initial_messages=(), change_context=_cc(tmp_path),
        user_chat_input_fn=None,
        transcript_dir=tmp_path / "td", fs_permissions=(),
        events_path=tmp_path / "e.jsonl", subagent_defs={}, tiers=(),
        max_tool_calls=7, agent_id="sub:abc", event_log=None,
        change_state=None, first_event_parent_id="tc-1",
        resolved_tiers=None, coordinator=None, subagent_depth=2)

    # System prompt = base persona + rendered body + the complete_step contract.
    assert inv.system_prompt.startswith(_AGENT_BASE_PROMPT)
    assert "RENDERED BODY" in inv.system_prompt
    assert "complete_step" in inv.system_prompt
    # Non-interactive -> a nudge continuation is wired (omission #1/#2 closed).
    assert inv.continuation_fn is not None
    # Ceiling resolved (omission #3 closed).
    assert inv.max_tool_calls == 7
    # Threaded fields land on the invocation.
    assert inv.subagent_depth == 2
    assert inv.agent_id == "sub:abc"
    assert inv.first_event_parent_id == "tc-1"
    assert inv.user_input_fn is None  # non-interactive


def test_build_agent_invocation_injects_bash_paths_note_when_bash_present(
    tmp_path: Path,
) -> None:
    # An agent holding `bash` gets the relative-paths note appended to its
    # body (between the rendered body and the completion contract), so every
    # bash-capable agent learns the rule without each prompt re-pasting it.
    from imp.engine.agent_run import _AGENT_BASE_PROMPT, build_agent_invocation

    inv = build_agent_invocation(
        agent_def=_agent(tools={"complete_step", "bash"}),
        provider_name="anthropic", model_name="m",
        rendered_system_prompt="RENDERED BODY",
        initial_messages=(), change_context=_cc(tmp_path),
        user_chat_input_fn=None,
        transcript_dir=tmp_path / "td", fs_permissions=(),
        events_path=tmp_path / "e.jsonl", subagent_defs={}, tiers=(),
        max_tool_calls=None, agent_id="step:p/s", event_log=None,
        change_state=None, first_event_parent_id=None,
        resolved_tiers=None, coordinator=None)

    assert inv.system_prompt.startswith(_AGENT_BASE_PROMPT)
    assert "RENDERED BODY" in inv.system_prompt
    assert "IMP_PROJECT_ROOT" in inv.system_prompt
    assert "relative to the project root" in inv.system_prompt
    # The note precedes the completion contract.
    assert inv.system_prompt.index("IMP_PROJECT_ROOT") < inv.system_prompt.index(
        "complete_step")


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
        transcript_dir=tmp_path / "td", fs_permissions=(),
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
        transcript_dir=tmp_path / "td", fs_permissions=(),
        events_path=tmp_path / "e.jsonl", subagent_defs={}, tiers=(),
        max_tool_calls=None, agent_id="step:p/s", event_log=None,
        change_state=None, first_event_parent_id=None,
        resolved_tiers=None, coordinator=None)

    assert inv.continuation_fn is not None
    # user_chat_input_fn is wired unconditionally (the wait_for_user / AWAITING_USER
    # resume input channel for any attended top-level agent).
    assert inv.user_input_fn is not None


def test_build_agent_invocation_threads_lint_command(tmp_path: Path) -> None:
    from imp.engine.agent_run import build_agent_invocation

    inv = build_agent_invocation(
        agent_def=_agent(), provider_name="anthropic", model_name="m",
        rendered_system_prompt="BODY", initial_messages=(),
        change_context=_cc(tmp_path),
        user_chat_input_fn=None, transcript_dir=tmp_path / "td",
        fs_permissions=(), events_path=tmp_path / "e.jsonl",
        subagent_defs={}, tiers=(), max_tool_calls=None,
        agent_id="step:p/s", event_log=None, change_state=None,
        first_event_parent_id=None, resolved_tiers=None, coordinator=None,
        lint_command="make lint")
    assert inv.lint_command == "make lint"


def test_build_agent_invocation_threads_test_command(tmp_path: Path) -> None:
    from imp.engine.agent_run import build_agent_invocation

    inv = build_agent_invocation(
        agent_def=_agent(), provider_name="anthropic", model_name="m",
        rendered_system_prompt="BODY", initial_messages=(),
        change_context=_cc(tmp_path),
        user_chat_input_fn=None, transcript_dir=tmp_path / "td",
        fs_permissions=(), events_path=tmp_path / "e.jsonl",
        subagent_defs={}, tiers=(), max_tool_calls=None,
        agent_id="step:p/s", event_log=None, change_state=None,
        first_event_parent_id=None, resolved_tiers=None, coordinator=None,
        test_command="make test")
    assert inv.test_command == "make test"


def _inv(tmp_path: Path):
    from imp.engine.agent_run import build_agent_invocation
    return build_agent_invocation(
        agent_def=_agent(), provider_name="p", model_name="m",
        rendered_system_prompt="B", initial_messages=(),
        change_context=_cc(tmp_path),
        user_chat_input_fn=None, transcript_dir=tmp_path / "td",
        fs_permissions=(), events_path=tmp_path / "e.jsonl",
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
        "category": "auth", "kind": "AuthError", "message": "401"}}])
    res = await drive_invocation(_inv(tmp_path), runner,
                                 coordinator=coord, log=None, emit_events=False)
    assert res.fatal is True
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
    assert result.error is not None and result.error.kind == "stalled"
    assert interrupted["called"] is True


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
        transcript_dir=tmp_path, fs_permissions=(), events_path=tmp_path / "e",
        subagent_defs={}, tiers=(), max_tool_calls=None, agent_id="step:p/s",
        event_log=None, change_state=None, first_event_parent_id=None,
        resolved_tiers=None, coordinator=None, **kw,
    )


def test_build_agent_invocation_prepends_base_persona(tmp_path: Path) -> None:
    # The fresh-assembly branch puts the always-on base persona FIRST, then the
    # rendered agent role, then bash note (iff bash), then the engine-seed note,
    # then the completion instruction — in that exact order.
    from imp.engine.agent_run import _AGENT_BASE_PROMPT, build_agent_invocation

    inv = build_agent_invocation(
        agent_def=_agent(tools={"complete_step", "bash"}),
        provider_name="anthropic", model_name="m",
        rendered_system_prompt="RENDERED BODY",
        initial_messages=(), change_context=_cc(tmp_path),
        user_chat_input_fn=None,
        transcript_dir=tmp_path / "td", fs_permissions=(),
        events_path=tmp_path / "e.jsonl", subagent_defs={}, tiers=(),
        max_tool_calls=None, agent_id="step:p/s", event_log=None,
        change_state=None, first_event_parent_id=None,
        resolved_tiers=None, coordinator=None)

    sp = inv.system_prompt
    assert sp.startswith(_AGENT_BASE_PROMPT)
    assert sp.index(_AGENT_BASE_PROMPT) < sp.index("RENDERED BODY")
    assert sp.index("RENDERED BODY") < sp.index("IMP_PROJECT_ROOT")
    assert sp.index("IMP_PROJECT_ROOT") < sp.index("<imp:begin/>")
    assert sp.index("<imp:begin/>") < sp.index("complete_step")


def test_build_invocation_override_is_used_verbatim(tmp_path: Path) -> None:
    inv = _build_inv(tmp_path, system_prompt_override="EXACT STORED PROMPT")
    assert inv.system_prompt == "EXACT STORED PROMPT"        # no notes appended


def test_build_invocation_no_override_assembles_with_notes(tmp_path: Path) -> None:
    from imp.engine.agent_run import _AGENT_BASE_PROMPT
    inv = _build_inv(tmp_path)
    assert inv.system_prompt.startswith(_AGENT_BASE_PROMPT)
    assert "BODY" in inv.system_prompt
    assert "Session start: `<imp:begin/>`" in inv.system_prompt   # seed note appended


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


def test_build_agent_invocation_appends_engine_seed_note_unconditionally(
    tmp_path: Path,
) -> None:
    """Every agent's system prompt carries the `<imp:begin/>` engine-seed note,
    regardless of whether it has the `bash` tool. The note sits between any
    body addenda and the completion contract."""
    from imp.engine.agent_run import _AGENT_BASE_PROMPT, build_agent_invocation

    # Without bash: still gets the engine-seed note.
    inv = build_agent_invocation(
        agent_def=_agent(),  # tools = {complete_step} — no bash
        provider_name="anthropic", model_name="m",
        rendered_system_prompt="RENDERED BODY",
        initial_messages=(), change_context=_cc(tmp_path),
        user_chat_input_fn=None,
        transcript_dir=tmp_path / "td", fs_permissions=(),
        events_path=tmp_path / "e.jsonl", subagent_defs={}, tiers=(),
        max_tool_calls=None, agent_id="step:p/s", event_log=None,
        change_state=None, first_event_parent_id=None,
        resolved_tiers=None, coordinator=None)

    assert "<imp:begin/>" in inv.system_prompt
    # The note explains the tag is engine-emitted, not user input.
    assert "engine" in inv.system_prompt.lower()
    # Order: base persona first, rendered body present, engine-seed note before completion contract.
    assert inv.system_prompt.startswith(_AGENT_BASE_PROMPT)
    assert "RENDERED BODY" in inv.system_prompt
    assert inv.system_prompt.index("<imp:begin/>") < inv.system_prompt.index(
        "complete_step")

    # With bash too: both notes appear; bash note precedes engine-seed note,
    # which in turn precedes the completion instruction.
    inv2 = build_agent_invocation(
        agent_def=_agent(tools={"complete_step", "bash"}),
        provider_name="anthropic", model_name="m",
        rendered_system_prompt="RENDERED BODY",
        initial_messages=(), change_context=_cc(tmp_path),
        user_chat_input_fn=None,
        transcript_dir=tmp_path / "td", fs_permissions=(),
        events_path=tmp_path / "e.jsonl", subagent_defs={}, tiers=(),
        max_tool_calls=None, agent_id="step:p/s", event_log=None,
        change_state=None, first_event_parent_id=None,
        resolved_tiers=None, coordinator=None)

    assert "IMP_PROJECT_ROOT" in inv2.system_prompt
    assert "<imp:begin/>" in inv2.system_prompt
    assert inv2.system_prompt.index("IMP_PROJECT_ROOT") < inv2.system_prompt.index(
        "<imp:begin/>")
    assert inv2.system_prompt.index("<imp:begin/>") < inv2.system_prompt.index(
        "complete_step")


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
    coord = _RecordingCoord()

    async def _ainput(_q):
        assert coord.active is True   # watchdog frozen DURING the await
        return "resumed reply"

    result = await ar._resolve_wait_reply(coord, _ainput)
    assert result == "resumed reply"
    assert coord.events == ["begin", "end"]
    assert coord.active is False


@pytest.mark.anyio
async def test_resolve_wait_reply_restores_watchdog_on_exception() -> None:
    import imp.engine.agent_run as ar
    coord = _RecordingCoord()

    async def _ainput(_q):
        raise RuntimeError("prompt torn down")

    with pytest.raises(RuntimeError):
        await ar._resolve_wait_reply(coord, _ainput)
    assert coord.events == ["begin", "end"]   # finally restored it on the exception
    assert coord.active is False


@pytest.mark.anyio
async def test_real_wait_for_user_solicit_is_not_torn_down_by_watchdog(
    tmp_path, monkeypatch,
) -> None:
    """Regression for the live ~35-min loop: an attended agent blocked at the
    REAL wait_for_user tool for far longer than the inactivity window is NOT
    killed by the watchdog. Once the human replies the turn continues. Drives the
    real drive_invocation -> _pull_with_inactivity_watchdog -> wait_for_user
    bracket -> coordinator freeze, deterministically (no LLM)."""
    import imp.engine.agent_run as ar
    from imp.agents.tools.context import ToolContext
    from imp.agents.tools.wait_for_user import make_wait_for_user_tool

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
            tool = make_wait_for_user_tool()
            ctx = ToolContext(
                project_root=tmp_path, fs_permissions=(),
                events_path=tmp_path / "e.jsonl",
                user_input_fn=_blocking_input, non_interactive=False,
                event_log=log, agent_id="step:p/s", coordinator=coord)
            captured["reply"] = await tool.async_invoke(ctx, {"prompt": "merge?"})
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
        and e["payload"].get("category") == "config"
        and e["payload"].get("kind") == "stalled"
    ]
    assert stalls == []                            # no false config/stalled teardown

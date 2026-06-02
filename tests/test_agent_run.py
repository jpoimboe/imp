from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.runner import ChangeContext
from imp.cli.signals import InterruptCoordinator
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _agent(name: str = "rev", max_tool_calls=None, subagents=()) -> AgentDef:
    return AgentDef(
        name=name, description="", tools=frozenset({"complete_step"}),
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
    from imp.engine.agent_run import build_agent_invocation

    inv = build_agent_invocation(
        agent_def=_agent(), provider_name="anthropic", model_name="m",
        rendered_system_prompt="RENDERED BODY",
        initial_messages=(), change_context=_cc(tmp_path),
        interactive=False, user_chat_input_fn=None,
        transcript_dir=tmp_path / "td", fs_permissions=(),
        events_path=tmp_path / "e.jsonl", subagent_defs={}, model_tiers=(),
        max_tool_calls=7, agent_id="sub:abc", event_log=None,
        change_state=None, first_event_parent_id="tc-1",
        resolved_roles=None, coordinator=None, subagent_depth=2)

    # System prompt = rendered body + the complete_step contract.
    assert inv.system_prompt.startswith("RENDERED BODY")
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


def test_build_agent_invocation_interactive_wires_user_input(tmp_path: Path) -> None:
    from imp.engine.agent_run import build_agent_invocation

    inv = build_agent_invocation(
        agent_def=_agent(), provider_name="anthropic", model_name="m",
        rendered_system_prompt="BODY", initial_messages=(),
        change_context=_cc(tmp_path), interactive=True,
        user_chat_input_fn=lambda _p: "hi",
        transcript_dir=tmp_path / "td", fs_permissions=(),
        events_path=tmp_path / "e.jsonl", subagent_defs={}, model_tiers=(),
        max_tool_calls=None, agent_id="step:p/s", event_log=None,
        change_state=None, first_event_parent_id=None,
        resolved_roles=None, coordinator=None)

    assert inv.interactive is True
    assert inv.continuation_fn is not None
    assert inv.user_input_fn is not None  # interactive -> PROMPT_USER resume wired


def test_build_agent_invocation_threads_lint_command(tmp_path: Path) -> None:
    from imp.engine.agent_run import build_agent_invocation

    inv = build_agent_invocation(
        agent_def=_agent(), provider_name="anthropic", model_name="m",
        rendered_system_prompt="BODY", initial_messages=(),
        change_context=_cc(tmp_path), interactive=False,
        user_chat_input_fn=None, transcript_dir=tmp_path / "td",
        fs_permissions=(), events_path=tmp_path / "e.jsonl",
        subagent_defs={}, model_tiers=(), max_tool_calls=None,
        agent_id="step:p/s", event_log=None, change_state=None,
        first_event_parent_id=None, resolved_roles=None, coordinator=None,
        lint_command="make lint")
    assert inv.lint_command == "make lint"


def test_build_agent_invocation_threads_test_command(tmp_path: Path) -> None:
    from imp.engine.agent_run import build_agent_invocation

    inv = build_agent_invocation(
        agent_def=_agent(), provider_name="anthropic", model_name="m",
        rendered_system_prompt="BODY", initial_messages=(),
        change_context=_cc(tmp_path), interactive=False,
        user_chat_input_fn=None, transcript_dir=tmp_path / "td",
        fs_permissions=(), events_path=tmp_path / "e.jsonl",
        subagent_defs={}, model_tiers=(), max_tool_calls=None,
        agent_id="step:p/s", event_log=None, change_state=None,
        first_event_parent_id=None, resolved_roles=None, coordinator=None,
        test_command="make test")
    assert inv.test_command == "make test"


def _inv(tmp_path: Path):
    from imp.engine.agent_run import build_agent_invocation
    return build_agent_invocation(
        agent_def=_agent(), provider_name="p", model_name="m",
        rendered_system_prompt="B", initial_messages=(),
        change_context=_cc(tmp_path), interactive=False,
        user_chat_input_fn=None, transcript_dir=tmp_path / "td",
        fs_permissions=(), events_path=tmp_path / "e.jsonl",
        subagent_defs={}, model_tiers=(), max_tool_calls=None,
        agent_id="step:p/s", event_log=None, change_state=None,
        first_event_parent_id=None, resolved_roles=None, coordinator=None)


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

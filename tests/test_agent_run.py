from __future__ import annotations

from pathlib import Path

from imp.agents.runner import ChangeContext
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc


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

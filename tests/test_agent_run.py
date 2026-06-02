from __future__ import annotations


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

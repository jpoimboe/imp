from pathlib import Path

import imp.agents.dispatch as dispatch_mod
import imp.agents.runner as runner_mod
import imp.agents.tools.context as context_mod
import imp.engine.agent_run as agent_run_mod
import imp.engine.executor as executor_mod


def test_context_source_has_no_wait_for_user_references():
    """context.py's refreshed comments hold no wait_for_user reference."""
    src = Path(context_mod.__file__).read_text()
    assert "wait_for_user" not in src


def test_runner_source_has_no_wait_for_user_references():
    """runner.py's refreshed comments hold no wait_for_user reference."""
    src = Path(runner_mod.__file__).read_text()
    assert "wait_for_user" not in src


def test_dispatch_pause_comment_drops_dangling_tool_use_framing():
    """dispatch.py's AwaitingUserInput unwind comment drops dangling-tool_use framing."""
    src = Path(dispatch_mod.__file__).read_text()
    assert "dangling wait_for_user tool_use" not in src
    assert "Headless wait_for_user pause" not in src


def test_executor_resume_comment_drops_dangling_tool_use_framing():
    """executor.py's resume-marker comment drops dangling-tool_use framing."""
    src = Path(executor_mod.__file__).read_text()
    assert "dangling wait_for_user tool_use" not in src


def test_agent_run_source_drops_wait_for_user_framing():
    """agent_run.py imports the pause signal from its post-rename home and holds
    no stale wait_for_user / dangling-tool_use framing at all."""
    src = Path(agent_run_mod.__file__).read_text()
    assert "wait_for_user" not in src
    assert "from imp.engine.awaiting_user_input import AwaitingUserInput" in src

from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.executor import _latest_step_context_system_prompt
from imp.engine.state import InMemoryEventLog


def test_latest_step_context_picks_the_last_for_agent_id() -> None:
    log = InMemoryEventLog()
    aid = "step:spec/spec-writer"
    log.append_step_context(agent_id=aid, provider="anthropic", model="m",
                            system_prompt="FIRST", tools_schema=[], params={},
                            max_tool_calls=None)
    log.append_step_context(agent_id=aid, provider="anthropic", model="m",
                            system_prompt="SECOND", tools_schema=[], params={},
                            max_tool_calls=None)
    # A different agent_id must not be picked up.
    log.append_step_context(agent_id="other", provider="anthropic", model="m",
                            system_prompt="NOPE", tools_schema=[], params={},
                            max_tool_calls=None)
    assert _latest_step_context_system_prompt(log, aid) == "SECOND"


def test_latest_step_context_none_when_absent() -> None:
    assert _latest_step_context_system_prompt(InMemoryEventLog(), "x") is None


def test_missing_copy_nonempty_transcript_aborts(tmp_path: Path) -> None:
    from imp.engine.executor import _resume_system_prompt
    from imp.agents.runner import Message, TextBlock
    from imp.agents.transcript import Transcript
    t = Transcript(tmp_path / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="<imp:begin/>"),)))
    with pytest.raises(RuntimeError):
        _resume_system_prompt(InMemoryEventLog(), "step:p/s", tmp_path)


def test_missing_copy_empty_transcript_rerenders(tmp_path: Path) -> None:
    from imp.engine.executor import _resume_system_prompt
    # Empty transcript + no step_context => None (caller re-renders), not an abort.
    assert _resume_system_prompt(InMemoryEventLog(), "step:p/s", tmp_path) is None


def test_resume_returns_stored_when_present(tmp_path: Path) -> None:
    from imp.engine.executor import _resume_system_prompt
    log = InMemoryEventLog()
    log.append_step_context(agent_id="step:p/s", provider="anthropic", model="m",
                            system_prompt="STORED", tools_schema=[], params={},
                            max_tool_calls=None)
    assert _resume_system_prompt(log, "step:p/s", tmp_path) == "STORED"

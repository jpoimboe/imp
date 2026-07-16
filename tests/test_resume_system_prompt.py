from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.executor import (
    _latest_engine_system_prompt,
    _resume_system_prompt,
)


class _Log:
    def __init__(self, events: list[dict]) -> None:
        self._events = events

    def read(self) -> list[dict]:
        return list(self._events)


def _engine_sys(agent_id: str, text: str) -> dict:
    return {"type": "engine_message", "agent_id": agent_id,
            "payload": {"role": "system", "subtype": "system_prompt",
                        "content": [{"type": "text", "text": text}]}}


def test_latest_engine_system_prompt_reads_engine_message() -> None:
    log = _Log([
        {"type": "step_context", "agent_id": "step:build/plan",
         "payload": {"provider": "anthropic", "model": "m"}},
        _engine_sys("step:build/plan", "You are a coder in imp."),
    ])
    assert (_latest_engine_system_prompt(log, "step:build/plan")
            == "You are a coder in imp.")


def test_step_context_system_prompt_is_not_a_source() -> None:
    # The prompt moved OFF step_context (plan 1). A stale system_prompt field on a
    # step_context must NOT be read back — only engine_message/system_prompt counts.
    log = _Log([
        {"type": "step_context", "agent_id": "step:build/plan",
         "payload": {"provider": "a", "model": "m", "system_prompt": "STALE"}},
    ])
    assert _latest_engine_system_prompt(log, "step:build/plan") is None


def test_latest_engine_system_prompt_takes_newest_for_agent() -> None:
    # goto re-entry emits a newer engine_message/system_prompt; resume replays it.
    log = _Log([
        _engine_sys("step:build/plan", "OLD ROLE PROMPT"),
        _engine_sys("step:build/plan", "NEW ROLE PROMPT"),
        _engine_sys("step:build/other", "OTHER AGENT"),
    ])
    assert (_latest_engine_system_prompt(log, "step:build/plan")
            == "NEW ROLE PROMPT")


def test_resume_system_prompt_returns_stored_without_touching_transcript(
        tmp_path: Path) -> None:
    log = _Log([_engine_sys("step:build/plan", "ROLE PROMPT")])
    # stored is not None → the transcript invariant branch is never reached.
    assert (_resume_system_prompt(log, "step:build/plan", tmp_path)
            == "ROLE PROMPT")


def test_resume_system_prompt_aborts_on_nonempty_transcript_without_stored(
        tmp_path: Path) -> None:
    from imp.agents.runner import Message, TextBlock
    from imp.agents.transcript import Transcript
    # No engine_message/system_prompt stored + a non-empty transcript => the resume
    # invariant fires: re-rendering against a now-lost prompt would silently diverge.
    t = Transcript(tmp_path / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="prior turn"),)))
    with pytest.raises(RuntimeError):
        _resume_system_prompt(_Log([]), "step:p/s", tmp_path)


def test_resume_system_prompt_rerenders_on_empty_transcript_without_stored(
        tmp_path: Path) -> None:
    # Empty transcript + no stored engine_message/system_prompt => None (re-render),
    # not an abort.
    assert _resume_system_prompt(_Log([]), "step:p/s", tmp_path) is None

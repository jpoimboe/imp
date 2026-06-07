# tests/test_codex_resume_state.py
from __future__ import annotations

from pathlib import Path

from imp.agents.classify import ResumeState
from imp.providers.codex_resume import derive_codex_resume_state


def _write_events(p: Path, records: list[dict]) -> None:
    import json
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_prompt_user_when_tail_is_llm_response_and_interactive(tmp_path: Path) -> None:
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [
        {"agent_id": "step:t", "type": "llm_response",
         "payload": {"raw": {"content": [{"type": "text", "text": "a question?"}]}}},
    ])
    state, last_text = derive_codex_resume_state(ev, "step:t", interactive=True)
    assert state is ResumeState.PROMPT_USER
    assert last_text == "a question?"


def test_missing_complete_step_when_not_interactive(tmp_path: Path) -> None:
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [
        {"agent_id": "step:t", "type": "llm_response",
         "payload": {"raw": {"content": [{"type": "text", "text": "done?"}]}}},
    ])
    state, _ = derive_codex_resume_state(ev, "step:t", interactive=False)
    assert state is ResumeState.MISSING_COMPLETE_STEP


def test_fresh_when_events_file_missing(tmp_path: Path) -> None:
    state, last_text = derive_codex_resume_state(
        tmp_path / "nope.jsonl", "step:t", interactive=True
    )
    assert state is ResumeState.FRESH
    assert last_text == ""


def test_fresh_when_no_records_for_agent(tmp_path: Path) -> None:
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [
        {"agent_id": "step:other", "type": "llm_response",
         "payload": {"raw": {"content": [{"type": "text", "text": "irrelevant"}]}}},
    ])
    state, last_text = derive_codex_resume_state(ev, "step:t", interactive=True)
    assert state is ResumeState.FRESH
    assert last_text == ""


def test_normal_resume_when_complete_step_present(tmp_path: Path) -> None:
    # Post-F1 emit order: the in-turn tool_call/tool_result come FIRST, then the
    # reassembled llm_response is the tail. Classification is presence-based, so
    # a completed step → SEND_TO_LLM regardless of llm_response position.
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [
        {"agent_id": "step:t", "type": "tool_call",
         "payload": {"name": "complete_step", "input": {}}},
        {"agent_id": "step:t", "type": "tool_result",
         "payload": {"tool_use_id": "x", "content": "step complete"}},
        {"agent_id": "step:t", "type": "llm_response",
         "payload": {"raw": {"content": [{"type": "text", "text": "answer"}]}}},
    ])
    state, _ = derive_codex_resume_state(ev, "step:t", interactive=True)
    assert state is ResumeState.SEND_TO_LLM  # nothing to prompt; resume normally

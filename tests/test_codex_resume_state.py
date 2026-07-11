# tests/test_codex_resume_state.py
from __future__ import annotations

from pathlib import Path

from imp.agents.classify import ResumeState
from imp.providers.codex_resume import (
    derive_codex_resume_state,
    pending_human_reply,
)


def _write_events(p: Path, records: list[dict]) -> None:
    import json
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_awaiting_user_when_awaiting_event_present(tmp_path: Path) -> None:
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [
        {"agent_id": "step:t", "type": "llm_response",
         "payload": {"raw": {"content": [{"type": "text", "text": "a question?"}]}}},
        {"agent_id": "step:t", "type": "awaiting_user_input",
         "payload": {"prompt": "a question?"}},
    ])
    state, last_text = derive_codex_resume_state(ev, "step:t")
    assert state is ResumeState.AWAITING_USER
    assert last_text == "a question?"


def test_missing_complete_step_without_awaiting(tmp_path: Path) -> None:
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [
        {"agent_id": "step:t", "type": "llm_response",
         "payload": {"raw": {"content": [{"type": "text", "text": "done?"}]}}},
    ])
    state, _ = derive_codex_resume_state(ev, "step:t")
    assert state is ResumeState.MISSING_COMPLETE_STEP


def test_fresh_when_events_file_missing(tmp_path: Path) -> None:
    state, last_text = derive_codex_resume_state(
        tmp_path / "nope.jsonl", "step:t"
    )
    assert state is ResumeState.FRESH
    assert last_text == ""


def test_fresh_when_no_records_for_agent(tmp_path: Path) -> None:
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [
        {"agent_id": "step:other", "type": "llm_response",
         "payload": {"raw": {"content": [{"type": "text", "text": "irrelevant"}]}}},
    ])
    state, last_text = derive_codex_resume_state(ev, "step:t")
    assert state is ResumeState.FRESH
    assert last_text == ""


def test_normal_resume_when_complete_step_present(tmp_path: Path) -> None:
    # complete_step present → the turn completed; resume normally (SEND_TO_LLM)
    # regardless of llm_response position. Classification is presence-based.
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [
        {"agent_id": "step:t", "type": "tool_call",
         "payload": {"name": "complete_step", "input": {}}},
        {"agent_id": "step:t", "type": "tool_result",
         "payload": {"tool_use_id": "x", "content": "step complete"}},
        {"agent_id": "step:t", "type": "llm_response",
         "payload": {"raw": {"content": [{"type": "text", "text": "answer"}]}}},
    ])
    state, _ = derive_codex_resume_state(ev, "step:t")
    assert state is ResumeState.SEND_TO_LLM  # nothing to prompt; resume normally


def test_subagent_bare_turn_end_resumes_without_nudge(tmp_path: Path) -> None:
    # A subagent (subagent_depth>0) never calls complete_step; its bare-turn-end
    # terminal IS the done-signal, so it resumes normally (SEND_TO_LLM) instead
    # of the top-level MISSING_COMPLETE_STEP replay-and-continue nudge.
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [
        {"agent_id": "sub:tc-1", "type": "llm_response",
         "payload": {"raw": {"content": [
             {"type": "text", "text": "the conclusion"}]}}},
    ])
    state, last_text = derive_codex_resume_state(
        ev, "sub:tc-1", subagent_depth=1)
    assert state is ResumeState.SEND_TO_LLM
    assert last_text == "the conclusion"


def test_subagent_awaiting_user_still_returns_awaiting(tmp_path: Path) -> None:
    # Ordering invariant: the subagent_depth>0 branch sits AFTER the has_awaiting
    # check, so a subagent that DID hit an awaiting-user signal still returns
    # AWAITING_USER — it is not overridden into SEND_TO_LLM.
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [
        {"agent_id": "sub:tc-1", "type": "llm_response",
         "payload": {"raw": {"content": [
             {"type": "text", "text": "the conclusion"}]}}},
        {"agent_id": "sub:tc-1", "type": "awaiting_user_input"},
    ])
    state, last_text = derive_codex_resume_state(
        ev, "sub:tc-1", subagent_depth=1)
    assert state is ResumeState.AWAITING_USER
    assert last_text == "the conclusion"


def _user_msg(text: str) -> dict:
    return {"agent_id": "step:t", "type": "user_message",
            "payload": {"role": "user",
                        "content": [{"type": "text", "text": text}]}}


def test_pending_reply_returns_solicited_text(tmp_path: Path) -> None:
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [
        {"agent_id": "step:t", "type": "llm_response",
         "payload": {"raw": {"content": [{"type": "text", "text": "which file?"}]}}},
        {"agent_id": "step:t", "type": "awaiting_user_input",
         "payload": {"prompt": "which file?"}},
        _user_msg("use config.py"),
    ])
    assert pending_human_reply(ev, "step:t") == "use config.py"


def test_pending_reply_none_when_agent_already_answered(tmp_path: Path) -> None:
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [
        {"agent_id": "step:t", "type": "awaiting_user_input",
         "payload": {"prompt": "which file?"}},
        _user_msg("use config.py"),
        {"agent_id": "step:t", "type": "llm_response",
         "payload": {"raw": {"content": [{"type": "text", "text": "ok, editing"}]}}},
    ])
    assert pending_human_reply(ev, "step:t") is None


def test_pending_reply_none_when_no_user_message_after_gate(tmp_path: Path) -> None:
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [
        {"agent_id": "step:t", "type": "llm_response",
         "payload": {"raw": {"content": [{"type": "text", "text": "which file?"}]}}},
        {"agent_id": "step:t", "type": "awaiting_user_input",
         "payload": {"prompt": "which file?"}},
    ])
    assert pending_human_reply(ev, "step:t") is None


def test_pending_reply_none_when_no_awaiting_gate(tmp_path: Path) -> None:
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [_user_msg("stray reply with no gate")])
    assert pending_human_reply(ev, "step:t") is None


def test_pending_reply_none_when_reply_is_whitespace(tmp_path: Path) -> None:
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [
        {"agent_id": "step:t", "type": "awaiting_user_input",
         "payload": {"prompt": "which file?"}},
        _user_msg("   \n  "),
    ])
    assert pending_human_reply(ev, "step:t") is None


def test_pending_reply_none_when_events_file_missing(tmp_path: Path) -> None:
    assert pending_human_reply(tmp_path / "nope.jsonl", "step:t") is None


def test_pending_reply_uses_last_gate_not_first(tmp_path: Path) -> None:
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [
        {"agent_id": "step:t", "type": "awaiting_user_input",
         "payload": {"prompt": "first?"}},
        _user_msg("first reply"),
        {"agent_id": "step:t", "type": "llm_response",
         "payload": {"raw": {"content": [{"type": "text", "text": "acting"}]}}},
        {"agent_id": "step:t", "type": "awaiting_user_input",
         "payload": {"prompt": "second?"}},
        _user_msg("second reply"),
    ])
    assert pending_human_reply(ev, "step:t") == "second reply"


def test_pending_reply_none_when_tool_call_after_reply(tmp_path: Path) -> None:
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [
        {"agent_id": "step:t", "type": "awaiting_user_input",
         "payload": {"prompt": "which file?"}},
        _user_msg("use config.py"),
        {"agent_id": "step:t", "type": "tool_call",
         "payload": {"name": "edit_file"}},
    ])
    assert pending_human_reply(ev, "step:t") is None

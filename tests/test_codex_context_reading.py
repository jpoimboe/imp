from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import imp.providers.codex_runner as cr
from imp.engine.state import InMemoryEventLog
from imp.providers.codex_runner import process_event
from tests.codex_fakes import agent_message, turn_completed

_WINDOW = 258_400


def _codex_home(tmp_path: Path, *, records: list[tuple[int, int]]) -> Path:
    """A CODEX_HOME whose rollout for thread `thr-1` carries one codex
    `token_count` record per (occupancy, window) pair. The distractor values
    (82628/82956 turn sums, 22290 input_tokens) pin which field is read."""
    home = tmp_path / "codex_home"
    dest = (home / "sessions" / "2026" / "06" / "05"
            / "rollout-2026-06-05T00-00-00-thr-1.jsonl")
    dest.parent.mkdir(parents=True)
    dest.write_text("".join(
        json.dumps({
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"input_tokens": 82628,
                                          "total_tokens": 82956},
                    "last_token_usage": {"input_tokens": 22290,
                                         "cached_input_tokens": 20992,
                                         "total_tokens": tokens},
                    "model_context_window": window,
                },
            },
        }) + "\n"
        for tokens, window in records))
    return home


def test_turn_completed_lands_the_rollout_reading(tmp_path: Path) -> None:
    home = _codex_home(tmp_path, records=[(22_389, _WINDOW)])
    elog = InMemoryEventLog()
    state: dict[str, Any] = {"thread_id": "thr-1", "auth_mode": "chatgpt"}
    process_event(agent_message("done"), elog=elog, agent_id="step:a",
                  state=state, provider="codex", model="gpt-5.6-luna",
                  codex_home=home)
    process_event(
        turn_completed(input_tokens=82_628, output_tokens=99,
                       cached_input_tokens=20_992, reasoning_output_tokens=70),
        elog=elog, agent_id="step:a", state=state, provider="codex",
        model="gpt-5.6-luna", codex_home=home)
    payload = next(e for e in elog.read()
                   if e["type"] == "agent_message")["payload"]
    # Occupancy comes from codex's own per-request record, NOT from the turn's
    # usage sum (which would give 82628).
    assert payload["context_tokens"] == 22_389
    assert payload["context_window"] == _WINDOW
    # The billing sum is untouched: fresh 61636 + cache_read 20992 = 82628.
    usage = payload["usage"]
    assert usage["input_tokens"] + usage["cache_read_input_tokens"] == 82_628
    assert state["context_reading"] == (22_389, _WINDOW)


def test_turn_completed_without_a_reading_omits_context_fields(
        tmp_path: Path) -> None:
    home = _codex_home(tmp_path, records=[])
    elog = InMemoryEventLog()
    state: dict[str, Any] = {"thread_id": "thr-1", "auth_mode": "chatgpt"}
    process_event(agent_message("done"), elog=elog, agent_id="step:a",
                  state=state, provider="codex", model="gpt-5.6-luna",
                  codex_home=home)
    process_event(
        turn_completed(input_tokens=82_628, output_tokens=99,
                       cached_input_tokens=20_992),
        elog=elog, agent_id="step:a", state=state, provider="codex",
        model="gpt-5.6-luna", codex_home=home)
    payload = next(e for e in elog.read()
                   if e["type"] == "agent_message")["payload"]
    assert "context_tokens" not in payload
    assert "context_window" not in payload
    assert payload["usage"]["output_tokens"] == 99
    assert state["context_reading"] is None


def test_unreadable_rollout_does_not_kill_the_turn(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """codex writes the rollout from another process while imp reads it, so the
    locator's rglob->stat window can lose the file. A gauge read must never take
    the turn down with it."""
    def _boom(codex_home: Path, thread_id: str) -> tuple[int, int] | None:
        raise OSError("rollout vanished mid-turn")

    home = _codex_home(tmp_path, records=[(22_389, _WINDOW)])
    monkeypatch.setattr(cr, "latest_context_reading", _boom)
    elog = InMemoryEventLog()
    state: dict[str, Any] = {"thread_id": "thr-1", "auth_mode": "chatgpt"}
    process_event(agent_message("done"), elog=elog, agent_id="step:a",
                  state=state, provider="codex", model="gpt-5.6-luna",
                  codex_home=home)
    process_event(
        turn_completed(input_tokens=82_628, output_tokens=99),
        elog=elog, agent_id="step:a", state=state, provider="codex",
        model="gpt-5.6-luna", codex_home=home)
    payload = next(e for e in elog.read()
                   if e["type"] == "agent_message")["payload"]
    # The turn still records its billing usage; only occupancy is missing.
    assert payload["usage"]["output_tokens"] == 99
    assert "context_tokens" not in payload
    assert state["context_reading"] is None


_CANARY = ("no codex context reading in the rollout; context gauge and "
           "handoff watch inactive for this turn (warned once per run)")


@dataclass
class _UsagelessTurn:
    """codex's usage-less `turn.completed` shape — the existing early-out."""
    type: str = "turn.completed"
    usage: None = None


def _drive_turn(elog: InMemoryEventLog, state: dict[str, Any],
                home: Path) -> None:
    process_event(agent_message("working"), elog=elog, agent_id="step:a",
                  state=state, provider="codex", model="gpt-5.6-luna",
                  codex_home=home)
    process_event(turn_completed(input_tokens=10), elog=elog,
                  agent_id="step:a", state=state, provider="codex",
                  model="gpt-5.6-luna", codex_home=home)


def test_missing_reading_emits_exactly_one_canary_per_run(
        tmp_path: Path) -> None:
    home = _codex_home(tmp_path, records=[])
    elog = InMemoryEventLog()
    state: dict[str, Any] = {"thread_id": "thr-1", "auth_mode": "chatgpt"}
    _drive_turn(elog, state, home)
    _drive_turn(elog, state, home)
    warns = [e["payload"]["message"] for e in elog.read()
             if e["type"] == "runner_warning"]
    assert warns == [_CANARY]


def test_present_reading_emits_no_canary(tmp_path: Path) -> None:
    home = _codex_home(tmp_path, records=[(22_389, _WINDOW)])
    elog = InMemoryEventLog()
    state: dict[str, Any] = {"thread_id": "thr-1", "auth_mode": "chatgpt"}
    _drive_turn(elog, state, home)
    assert [e for e in elog.read() if e["type"] == "runner_warning"] == []


def test_usage_less_turn_emits_no_canary_and_no_patch(tmp_path: Path) -> None:
    home = _codex_home(tmp_path, records=[])
    elog = InMemoryEventLog()
    state: dict[str, Any] = {"thread_id": "thr-1", "auth_mode": "chatgpt"}
    process_event(agent_message("working"), elog=elog, agent_id="step:a",
                  state=state, provider="codex", model="gpt-5.6-luna",
                  codex_home=home)
    process_event(_UsagelessTurn(), elog=elog, agent_id="step:a", state=state,
                  provider="codex", model="gpt-5.6-luna", codex_home=home)
    assert [e for e in elog.read() if e["type"] == "runner_warning"] == []
    payload = next(e for e in elog.read()
                   if e["type"] == "agent_message")["payload"]
    assert payload["usage"] == {}
    assert "context_tokens" not in payload
    # The early-out ran before the read: no reading was even attempted.
    assert "context_reading" not in state

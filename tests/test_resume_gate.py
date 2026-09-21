# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""The event-log predicate behind both the inert-resume reprint and the daemon's
crash disposition."""
from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.resume_gate import unanswered_bare_gate
from tests.test_inert_resume import _gate, _seed_quest_with_events


def test_returns_the_payload_when_the_gate_carries_no_agent_message(
        tmp_path: Path) -> None:
    # The reason the predicate exists: a gate whose last_agent_message is empty is
    # still a gate the human never answered, where bare_turn_end_question is None.
    ch = _seed_quest_with_events(tmp_path, [_gate("")])
    assert unanswered_bare_gate(ch) == {
        "steer_hint": "", "last_agent_message": "", "interrupt": False}


def test_returns_the_payload_for_an_unanswered_question(tmp_path: Path) -> None:
    ch = _seed_quest_with_events(tmp_path, [_gate("Which DB should I use?")])
    assert unanswered_bare_gate(ch) == {
        "steer_hint": "", "last_agent_message": "Which DB should I use?",
        "interrupt": False}


def test_none_for_an_interrupt_gate(tmp_path: Path) -> None:
    ch = _seed_quest_with_events(
        tmp_path, [_gate("mid-work prose", interrupt=True)])
    assert unanswered_bare_gate(ch) is None


@pytest.mark.parametrize("answer", [
    {"type": "step_started", "payload": {"step": "s"}},
    {"type": "step_completed", "payload": {}},
    {"type": "user_message", "payload": {"subtype": "reply"}},
    {"type": "user_message", "payload": {"subtype": "interject"}},
])
def test_none_once_an_answer_follows_the_gate(
        tmp_path: Path, answer: dict) -> None:
    ch = _seed_quest_with_events(tmp_path, [_gate("Which DB?"), answer])
    assert unanswered_bare_gate(ch) is None


def test_an_opening_intent_does_not_answer_the_gate(tmp_path: Path) -> None:
    ch = _seed_quest_with_events(tmp_path, [
        _gate("Which DB?"),
        {"type": "user_message", "payload": {"subtype": "intent"}},
    ])
    assert unanswered_bare_gate(ch) == {
        "steer_hint": "", "last_agent_message": "Which DB?", "interrupt": False}


def test_workflow_resumed_does_not_answer_the_gate(tmp_path: Path) -> None:
    # The daemon appends workflow_resumed BEFORE reading the predicate
    # (daemon.py:187), so a leave/re-entry marker must leave the gate unanswered.
    ch = _seed_quest_with_events(tmp_path, [
        _gate("Which DB?"), {"type": "workflow_resumed", "payload": {}}])
    assert unanswered_bare_gate(ch) == {
        "steer_hint": "", "last_agent_message": "Which DB?", "interrupt": False}


def test_none_when_the_log_holds_no_gate(tmp_path: Path) -> None:
    ch = _seed_quest_with_events(
        tmp_path, [{"type": "step_started", "payload": {"step": "s"}}])
    assert unanswered_bare_gate(ch) is None


def test_only_the_last_gate_decides(tmp_path: Path) -> None:
    ch = _seed_quest_with_events(tmp_path, [
        _gate("earlier question?"), _gate("interrupt steer", interrupt=True)])
    assert unanswered_bare_gate(ch) is None

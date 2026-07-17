from __future__ import annotations

from imp.cli.main import (
    _first_genuine_human_turn,
    _prompt_history_lines,
    _resolve_intent,
)


def _um(subtype: str, text: str) -> dict:
    return {"type": "user_message", "agent_id": None,
            "payload": {"role": "user", "subtype": subtype,
                        "content": [{"type": "text", "text": text}]}}


def test_resolve_intent_reads_user_message_intent() -> None:
    assert _resolve_intent([_um("intent", "make search faster")]) \
        == "make search faster"


def test_resolve_intent_falls_back_to_first_reply() -> None:
    # No intent event: the opening is inferred from the first genuine human turn.
    assert _resolve_intent([_um("reply", "add a 300s timeout")]) \
        == "add a 300s timeout"


def test_first_genuine_human_turn_skips_the_intent() -> None:
    events = [_um("intent", "the opening"), _um("reply", "first real answer")]
    assert _first_genuine_human_turn(events) == "first real answer"


def test_first_genuine_human_turn_returns_interject() -> None:
    events = [_um("intent", "the opening"), _um("interject", "wait, sqlite")]
    assert _first_genuine_human_turn(events) == "wait, sqlite"


def test_first_genuine_human_turn_none_when_only_intent() -> None:
    assert _first_genuine_human_turn([_um("intent", "just the opening")]) is None


def test_resolve_intent_blank_intent_falls_back_to_reply() -> None:
    events = [_um("intent", "   "), _um("reply", "the real ask")]
    assert _resolve_intent(events) == "the real ask"


def test_prompt_history_includes_replies_and_interjects_not_intent() -> None:
    lines = _prompt_history_lines("seed opening", [
        _um("intent", "seed opening"),
        _um("reply", "use postgres"),
        _um("interject", "wait, sqlite"),
    ])
    assert lines == ["seed opening", "use postgres", "wait, sqlite"]

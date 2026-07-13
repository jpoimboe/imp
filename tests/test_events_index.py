from __future__ import annotations

from typing import Any

from imp.engine import events_index as ei


def _ev(
    eid: str | None,
    etype: str,
    agent_id: str | None = None,
    **payload: Any,
) -> dict[str, Any]:
    """An events.jsonl-shaped event dict. A `timestamp` kwarg is hoisted to the
    top-level field; everything else goes in `payload`."""
    ts = payload.pop("timestamp", None)
    event: dict[str, Any] = {
        "id": eid,
        "parent_id": None,
        "agent_id": agent_id,
        "type": etype,
        "payload": payload,
    }
    if ts is not None:
        event["timestamp"] = ts
    return event


def _indexed(events: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    return list(enumerate(events, start=1))


def test_is_suppressed_flags_malformed_and_usage_patch() -> None:
    assert ei.is_suppressed({"_malformed": True, "raw": "{bad"}) is True
    assert ei.is_suppressed({"type": "llm_response_usage_patch"}) is True
    assert ei.is_suppressed({"type": "tool_call"}) is False


def test_coalesce_ranges_merges_adjacent_and_splits_gaps() -> None:
    assert ei.coalesce_ranges([3, 1, 2, 5, 6, 9]) == [(1, 3), (5, 6), (9, 9)]
    assert ei.coalesce_ranges([]) == []
    assert ei.coalesce_ranges([4, 4, 4]) == [(4, 4)]


def test_format_ranges_uses_hash_prefix_and_dash() -> None:
    assert ei.format_ranges([(59, 120), (210, 255)]) == "#59-120, #210-255"
    assert ei.format_ranges([(42, 42)]) == "#42"

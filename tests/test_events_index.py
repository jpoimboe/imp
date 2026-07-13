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


def test_parse_range_spec_single_list_and_ranges() -> None:
    assert ei.parse_range_spec("42", max_ordinal=500) == [(42, 42)]
    assert ei.parse_range_spec("42,55,60", max_ordinal=500) == [
        (42, 42), (55, 55), (60, 60)]
    assert ei.parse_range_spec("59-120,210-255", max_ordinal=500) == [
        (59, 120), (210, 255)]


def test_parse_range_spec_open_ends_resolve_against_max() -> None:
    assert ei.parse_range_spec("59-", max_ordinal=300) == [(59, 300)]
    assert ei.parse_range_spec("-80", max_ordinal=300) == [(1, 80)]


def test_parse_range_spec_tolerates_hash_and_whitespace() -> None:
    assert ei.parse_range_spec(" #59-120 , #210-255 ", max_ordinal=999) == [
        (59, 120), (210, 255)]


def test_in_intervals_membership() -> None:
    intervals = [(59, 120), (210, 255)]
    assert ei.in_intervals(59, intervals) is True
    assert ei.in_intervals(255, intervals) is True
    assert ei.in_intervals(200, intervals) is False


def test_format_ranges_round_trips_through_parse() -> None:
    ranges = [(59, 120), (210, 255)]
    text = ei.format_ranges(ranges)
    assert ei.parse_range_spec(text, max_ordinal=999) == ranges

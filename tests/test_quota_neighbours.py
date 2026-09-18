from __future__ import annotations

import dataclasses
import json

from imp.cli.quota_neighbours import neighbour_slice

_START = "2026-06-01T03:30:00+00:00"
_END = "2026-06-01T05:38:00+00:00"


def _qr(ts: str, *, aid: str = "step:a", session: str = "s1",
        eid: str = "qr") -> dict:
    return {"id": eid, "agent_id": aid, "timestamp": ts, "type": "query_result",
            "payload": {"session_id": session, "duration_ms": 30000,
                        "total_cost_usd": 0.12, "usage": {}, "model_usage": {},
                        "num_turns": 3, "is_error": False}}


def _msg(ts: str, *, aid: str = "step:a", provider: str = "codex",
         model: str = "gpt-5.6-sol", auth: str = "chatgpt",
         eid: str = "am") -> dict:
    # `raw` is the verbatim provider response; its first block is the model's
    # thinking, which is what must never reach a neighbour's slice
    return {"id": eid, "agent_id": aid, "timestamp": ts, "type": "agent_message",
            "payload": {"provider": provider, "model": model, "auth_mode": auth,
                        "usage": {"input_tokens": 10, "output_tokens": 2},
                        "stop_reason": None,
                        "raw": {"content": [{"type": "thinking",
                                             "thinking": "SECRETPROSE"}]}}}


def _patch(*, aid: str = "step:a", tokens: int = 300) -> dict:
    return {"type": "agent_message_usage_patch", "agent_id": aid,
            "payload": {"usage": {"input_tokens": tokens, "output_tokens": 50,
                                  "cache_read_input_tokens": 600,
                                  "cache_creation_input_tokens": 100,
                                  "reasoning_output_tokens": 20}}}


def _reading(ts: str, *, used: float = 78.0, eid: str = "qk") -> dict:
    return {"id": eid, "agent_id": "step:a", "timestamp": ts,
            "type": "quota_reading",
            "payload": {"provider": "claude", "plan": None,
                        "windows": [{"label": "7d", "used_pct": used,
                                     "resets_at": 1788971433}], "raw": {}}}


def _prose(ts: str) -> dict:
    return {"id": "noise", "agent_id": "step:a", "timestamp": ts,
            "type": "tool_call",
            "payload": {"name": "bash", "input": {"cmd": "SECRETPROSE"}}}


# ---------------------------------------------------------------- the slice


def test_slice_keeps_only_in_span_readings_and_usage() -> None:
    got = neighbour_slice("proj", "n1", [
        _reading("2026-06-01T03:00:00+00:00", eid="before"),
        _reading("2026-06-01T04:00:00+00:00", eid="inside"),
        _reading("2026-06-01T06:00:00+00:00", eid="after"),
        _prose("2026-06-01T04:00:01+00:00"),
        _qr("2026-06-01T04:10:00+00:00", eid="qr-in"),
        _qr("2026-06-01T06:10:00+00:00", eid="qr-after"),
    ], _START, _END)
    assert got is not None
    assert (got.project, got.quest_id) == ("proj", "n1")
    assert [r["id"] for r in got.readings] == ["inside"]
    assert [u["id"] for u in got.usage] == ["qr-in"]
    assert got.anchors == ()


def test_the_slice_carries_only_the_fields_the_units_pipeline_reads() -> None:
    got = neighbour_slice("proj", "n1", [
        _msg("2026-06-01T04:00:00+00:00", eid="msg-in"),
        _qr("2026-06-01T04:10:00+00:00", eid="qr-in"),
        _reading("2026-06-01T04:20:00+00:00", eid="qk-in"),
    ], _START, _END)
    assert got is not None
    msg, qr = got.usage
    assert sorted(msg["payload"]) == ["auth_mode", "model", "provider", "usage"]
    assert sorted(qr["payload"]) == [
        "duration_ms", "model_usage", "session_id", "total_cost_usd", "usage"]
    assert sorted(got.readings[0]["payload"]) == ["provider", "windows"]
    assert sorted(msg) == ["agent_id", "id", "payload", "timestamp", "type"]
    # the model's thinking text lives in payload.raw and must not cross the seam
    assert "SECRETPROSE" not in json.dumps(dataclasses.asdict(got))
    # what IS needed survives intact
    assert msg["payload"]["usage"] == {"input_tokens": 10, "output_tokens": 2}
    assert msg["payload"]["provider"] == "codex"


def test_the_anchor_is_the_last_pre_span_row_of_a_pair_that_runs_in_span() -> None:
    got = neighbour_slice("proj", "n1", [
        _qr("2026-06-01T03:00:00+00:00", eid="early", session="s1"),
        _qr("2026-06-01T03:20:00+00:00", eid="anchor", session="s1"),
        _qr("2026-06-01T03:40:00+00:00", eid="in", session="s1"),
        _qr("2026-06-01T03:25:00+00:00", eid="unused", session="s2"),
    ], _START, _END)
    assert got is not None
    assert [a["id"] for a in got.anchors] == ["anchor"]
    assert [u["id"] for u in got.usage] == ["in"]


def test_a_patch_row_takes_the_span_of_its_position_in_the_log() -> None:
    got = neighbour_slice("proj", "n1", [
        _msg("2026-06-01T03:00:00+00:00", aid="step:before", eid="msg-before"),
        _patch(aid="step:before", tokens=111),
        _msg("2026-06-01T04:00:00+00:00", aid="step:inside", eid="msg-inside"),
        _patch(aid="step:inside", tokens=222),
    ], _START, _END)
    assert got is not None
    assert [u.get("id", u["type"]) for u in got.usage] == [
        "msg-inside", "agent_message_usage_patch"]
    assert got.usage[1]["payload"]["usage"]["input_tokens"] == 222


def test_a_patch_is_in_span_though_the_message_it_patches_precedes_it() -> None:
    """AC 21(g): a codex turn's usage lands at `turn.completed`, which can fall
    inside the span while the `agent_message` carrying it fell before the start.
    The patch is the row that bills, so dropping it under-counts the neighbour."""
    got = neighbour_slice("proj", "n1", [
        _msg("2026-06-01T03:00:00+00:00", eid="msg-pre-span"),
        _prose("2026-06-01T04:00:00+00:00"),   # the turn's tool work, in span
        _patch(tokens=777),
    ], _START, _END)
    assert got is not None
    assert [u.get("id", u["type"]) for u in got.usage] == [
        "agent_message_usage_patch"]
    assert got.usage[0]["payload"]["usage"]["input_tokens"] == 777
    # priced from the pre-span message's triple, which the slice still resolves
    assert got.agents == {"step:a": ("codex", "gpt-5.6-sol", "chatgpt")}


def test_the_triple_is_the_last_message_at_or_before_the_span_end() -> None:
    got = neighbour_slice("proj", "n1", [
        _msg("2026-06-01T03:00:00+00:00", provider="claude",
             model="claude-opus-5", auth="subscription", eid="pre"),
        _qr("2026-06-01T04:00:00+00:00", eid="qr-in"),
        _msg("2026-06-01T06:00:00+00:00", provider="claude",
             model="claude-haiku-4-5", auth="subscription", eid="post"),
    ], _START, _END)
    assert got is not None
    assert got.agents == {"step:a": ("claude", "claude-opus-5", "subscription")}
    assert [u["id"] for u in got.usage] == ["qr-in"]


def test_a_neighbour_with_an_in_span_reading_and_no_usage_is_still_returned() -> None:
    got = neighbour_slice("proj", "n1", [
        _reading("2026-06-01T04:00:00+00:00", eid="inside"),
        _qr("2026-06-01T06:10:00+00:00", eid="qr-after"),
    ], _START, _END)
    assert got is not None
    assert [r["id"] for r in got.readings] == ["inside"]
    assert got.usage == ()


def test_a_quest_with_no_in_span_activity_yields_no_slice() -> None:
    assert neighbour_slice("proj", "n1", [
        _reading("2026-06-01T01:00:00+00:00"),
        _qr("2026-06-01T01:05:00+00:00"),
    ], _START, _END) is None

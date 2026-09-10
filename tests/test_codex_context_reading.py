from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

import imp.providers.codex_runner as cr
from imp.engine.state import InMemoryEventLog
from imp.providers.codex_rollout import (
    CorrelatedReadings,
    RolloutCursor,
    RolloutReadings,
    RolloutTelemetry,
    RolloutUsage,
    quota_from_rate_limits,
)
from imp.providers.codex_runner import _rollout_usage, process_event
from tests.codex_fakes import agent_message, turn_completed

_WINDOW = 258_400


def test_rollout_usage_normalizes_all_completed_usage_fields() -> None:
    usage = _rollout_usage(SimpleNamespace(
        input_tokens=8, cached_input_tokens=None, cache_write_input_tokens=2,
        output_tokens=None, reasoning_output_tokens=3,
    ))
    assert (usage.input_tokens, usage.cached_input_tokens,
            usage.cache_write_input_tokens, usage.output_tokens,
            usage.reasoning_output_tokens) == (8, 0, 2, 0, 3)


def _codex_home(tmp_path: Path, *, records: list[tuple[int, int]],
                rate_limits: object = None) -> Path:
    """A CODEX_HOME whose rollout for thread `thr-1` carries one codex
    `token_count` record per (occupancy, window) pair, each with the same
    `rate_limits` block codex stamps on it. The distractor values (82628/82956
    turn sums, 22290 input_tokens) pin which field is read. Re-callable: a
    second call re-plants the rollout in place."""
    home = tmp_path / "codex_home"
    dest = (home / "sessions" / "2026" / "06" / "05"
            / "rollout-2026-06-05T00-00-00-thr-1.jsonl")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("".join(
        json.dumps({
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 82628,
                        "cached_input_tokens": 20992,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 328,
                        "reasoning_output_tokens": 70,
                        "total_tokens": 82956,
                    },
                    "last_token_usage": {"input_tokens": 22290,
                                         "cached_input_tokens": 20992,
                                         "total_tokens": tokens},
                    "model_context_window": window,
                },
                "rate_limits": rate_limits,
            },
        }) + "\n"
        for tokens, window in records))
    return home


_LIMITS: dict[str, Any] = {
    "primary": {"used_percent": 34.0, "window_minutes": 300, "resets_at": 1788403732},
    "secondary": {"used_percent": 7.0, "window_minutes": 10080, "resets_at": 1788971433},
    "plan_type": "plus",
}


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
        model="gpt-5.6-luna", codex_home=home,
        rollout_readings=_readings(22_389))
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


_CANARY = ("no codex context reading in the rollout; context gauge and "
           "handoff watch inactive for this turn (warned once per run)")


@dataclass
class _UsagelessTurn:
    """codex's usage-less `turn.completed` shape — the existing early-out."""
    type: str = "turn.completed"
    usage: None = None


def _readings(tokens: object, window: object = _WINDOW,
              rate_limits: object = None) -> RolloutReadings:
    context = ((tokens, window)
               if (isinstance(tokens, int) and not isinstance(tokens, bool) and tokens > 0
                   and isinstance(window, int) and not isinstance(window, bool) and window > 0)
               else None)
    return RolloutReadings(context=context, quota=quota_from_rate_limits(rate_limits))


def _completed_usage(
    *, input_tokens: int = 34_552, cached_input_tokens: int = 26_112,
    cache_write_input_tokens: int = 0, output_tokens: int = 85,
    reasoning_output_tokens: int = 29,
) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens, cached_input_tokens=cached_input_tokens,
        cache_write_input_tokens=cache_write_input_tokens,
        output_tokens=output_tokens, reasoning_output_tokens=reasoning_output_tokens,
    )


def _append_token_count(
    home: Path, usage: SimpleNamespace, occupancy: int, *, rate_limits: object = None,
    window: object = _WINDOW,
) -> None:
    rollout = next((home / "sessions").rglob("*.jsonl"))
    with rollout.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": usage.input_tokens,
                        "cached_input_tokens": usage.cached_input_tokens,
                        "cache_write_input_tokens": usage.cache_write_input_tokens,
                        "output_tokens": usage.output_tokens,
                        "reasoning_output_tokens": usage.reasoning_output_tokens,
                    },
                    "last_token_usage": {"total_tokens": occupancy},
                    "model_context_window": window,
                },
                "rate_limits": rate_limits,
            },
        }) + "\n")


def _drive_turn(elog: InMemoryEventLog, state: dict[str, Any],
                home: Path, readings: RolloutReadings) -> None:
    process_event(agent_message("working"), elog=elog, agent_id="step:a",
                  state=state, provider="codex", model="gpt-5.6-luna",
                  codex_home=home)
    process_event(turn_completed(input_tokens=10), elog=elog,
                  agent_id="step:a", state=state, provider="codex",
                  model="gpt-5.6-luna", codex_home=home,
                  rollout_readings=readings)


def test_missing_reading_emits_exactly_one_canary_per_run(
        tmp_path: Path) -> None:
    home = _codex_home(tmp_path, records=[])
    elog = InMemoryEventLog()
    state: dict[str, Any] = {"thread_id": "thr-1", "auth_mode": "chatgpt"}
    _drive_turn(elog, state, home, _readings(None))
    _drive_turn(elog, state, home, _readings(None))
    warns = [e["payload"]["message"] for e in elog.read()
             if e["type"] == "runner_warning"]
    assert warns == [_CANARY]


def test_present_reading_emits_no_canary(tmp_path: Path) -> None:
    home = _codex_home(tmp_path, records=[(22_389, _WINDOW)])
    elog = InMemoryEventLog()
    state: dict[str, Any] = {"thread_id": "thr-1", "auth_mode": "chatgpt"}
    _drive_turn(elog, state, home, _readings(22_389))
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


_COMPACTION_CANARY = (
    "codex auto-compacted its context — the model_auto_compact_token_limit "
    "pin is not in effect (warned once per run)")


def _compact(home: Path) -> None:
    """Append codex's top-level compaction record to the planted rollout."""
    rollout = next((home / "sessions").rglob("*.jsonl"))
    with rollout.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "timestamp": "2026-09-04T06:58:53.348Z",
            "ordinal": 18,
            "type": "compacted",
            "payload": {"message": "", "replacement_history": []},
        }) + "\n")


def test_compacted_rollout_emits_exactly_one_canary_per_run(
        tmp_path: Path) -> None:
    home = _codex_home(tmp_path, records=[(22_389, _WINDOW)])
    _compact(home)
    elog = InMemoryEventLog()
    state: dict[str, Any] = {"thread_id": "thr-1", "auth_mode": "chatgpt"}
    _drive_turn(elog, state, home, _readings(22_389))
    _drive_turn(elog, state, home, _readings(22_389))
    warns = [e["payload"]["message"] for e in elog.read()
             if e["type"] == "runner_warning"]
    assert warns == [_COMPACTION_CANARY]
    assert state["compaction_canary_emitted"] is True
    # The occupancy reading still lands: the canary is advisory, not a bail-out.
    payload = next(e for e in elog.read()
                   if e["type"] == "agent_message")["payload"]
    assert payload["context_tokens"] == 22_389


def test_non_compacted_rollout_warns_only_about_the_missing_reading(
        tmp_path: Path) -> None:
    home = _codex_home(tmp_path, records=[])
    elog = InMemoryEventLog()
    state: dict[str, Any] = {"thread_id": "thr-1", "auth_mode": "chatgpt"}
    _drive_turn(elog, state, home, _readings(None))
    warns = [e["payload"]["message"] for e in elog.read()
             if e["type"] == "runner_warning"]
    assert warns == [_CANARY]
    payload = next(e for e in elog.read()
                   if e["type"] == "agent_message")["payload"]
    # `_drive_turn` bills 10 fresh input tokens; the usage patch is untouched.
    assert payload["usage"]["input_tokens"] == 10


def test_unreadable_rollout_does_not_kill_the_compaction_check(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """codex writes the rollout from another process; the locator's rglob->stat
    window can lose the file mid-scan."""
    def _boom(codex_home: Path, thread_id: str) -> bool:
        raise OSError("rollout vanished mid-turn")

    home = _codex_home(tmp_path, records=[(22_389, _WINDOW)])
    _compact(home)
    monkeypatch.setattr(cr, "compaction_seen", _boom)
    elog = InMemoryEventLog()
    state: dict[str, Any] = {"thread_id": "thr-1", "auth_mode": "chatgpt"}
    _drive_turn(elog, state, home, _readings(22_389))
    assert [e for e in elog.read() if e["type"] == "runner_warning"] == []
    payload = next(e for e in elog.read()
                   if e["type"] == "agent_message")["payload"]
    assert payload["context_tokens"] == 22_389


def test_repeated_turns_record_one_quota_reading(tmp_path: Path) -> None:
    home = _codex_home(tmp_path, records=[(22_389, _WINDOW)], rate_limits=_LIMITS)
    elog = InMemoryEventLog()
    state: dict[str, Any] = {"thread_id": "thr-1", "auth_mode": "chatgpt"}
    _drive_turn(elog, state, home, _readings(22_389, rate_limits=_LIMITS))
    _drive_turn(elog, state, home, _readings(22_389, rate_limits=_LIMITS))
    quota = [e for e in elog.read() if e["type"] == "quota_reading"]
    assert len(quota) == 1
    assert quota[0]["payload"]["plan"] == "plus"
    assert [(w["label"], w["used_pct"], w["resets_at"])
            for w in quota[0]["payload"]["windows"]] == [
        ("5h", 34.0, 1788403732), ("7d", 7.0, 1788971433)]
    # The carrier's block reaches the recorded event unmodified, key for key.
    assert quota[0]["payload"]["raw"] == _LIMITS


def test_a_moved_reading_records_a_second_quota_event(tmp_path: Path) -> None:
    home = _codex_home(tmp_path, records=[(22_389, _WINDOW)], rate_limits=_LIMITS)
    elog = InMemoryEventLog()
    state: dict[str, Any] = {"thread_id": "thr-1", "auth_mode": "chatgpt"}
    _drive_turn(elog, state, home, _readings(22_389, rate_limits=_LIMITS))
    moved = {**_LIMITS, "primary": {**_LIMITS["primary"], "used_percent": 36.0}}
    _codex_home(tmp_path, records=[(22_500, _WINDOW)], rate_limits=moved)
    _drive_turn(elog, state, home, _readings(22_500, rate_limits=moved))
    pcts = [e["payload"]["windows"][0]["used_pct"] for e in elog.read()
            if e["type"] == "quota_reading"]
    assert pcts == [34.0, 36.0]


def test_turn_completed_lands_the_quota_reading_in_proc_state(tmp_path: Path) -> None:
    home = _codex_home(tmp_path, records=[(22_389, _WINDOW)], rate_limits=_LIMITS)
    elog = InMemoryEventLog()
    state: dict[str, Any] = {"thread_id": "thr-1", "auth_mode": "chatgpt",
                            "quota_reading": None}
    _drive_turn(elog, state, home, _readings(22_389, rate_limits=_LIMITS))
    assert state["quota_reading"].windows[0].label == "5h"
    # Still held on the repeat turn, where nothing is appended.
    _drive_turn(elog, state, home, _readings(22_389, rate_limits=_LIMITS))
    assert state["quota_reading"].windows[0].used_pct == 34.0


def test_a_rollout_without_rate_limits_records_no_quota_event(tmp_path: Path) -> None:
    home = _codex_home(tmp_path, records=[(22_389, _WINDOW)])
    elog = InMemoryEventLog()
    state: dict[str, Any] = {"thread_id": "thr-1", "auth_mode": "chatgpt",
                            "quota_reading": None}
    _drive_turn(elog, state, home, _readings(22_389))
    assert [e for e in elog.read() if e["type"] == "quota_reading"] == []
    assert state["quota_reading"] is None
    payload = next(e for e in elog.read()
                   if e["type"] == "agent_message")["payload"]
    assert payload["context_tokens"] == 22_389


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_wait_selects_immediate_current_record_and_stops_without_quota(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _codex_home(tmp_path, records=[(12_000, _WINDOW)])
    telemetry = RolloutTelemetry(home, "thr-1")
    cursor = telemetry.begin_turn()
    usage = _rollout_usage(_completed_usage())
    _append_token_count(home, _completed_usage(), 17_447)

    async def no_sleep(_: float) -> None:
        raise AssertionError("immediate reading must not poll")

    monkeypatch.setattr(cr.asyncio, "sleep", no_sleep)

    readings = await cr._wait_for_current_readings(telemetry, cursor, usage)

    assert (readings.context, readings.quota) == ((17_447, _WINDOW), None)


@pytest.mark.anyio
async def test_wait_observes_record_on_third_poll_then_stops_on_that_scan(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _codex_home(tmp_path, records=[])
    raw_usage = _completed_usage()
    _append_token_count(home, raw_usage, 12_000)
    telemetry = RolloutTelemetry(home, "thr-1")
    cursor = telemetry.begin_turn()
    usage = _rollout_usage(raw_usage)
    now = [10.0]
    scans: list[float] = []
    delays: list[float] = []
    third_poll = anyio.Event()
    appended = anyio.Event()
    scan_current = telemetry.scan_current

    def scan(_: RolloutTelemetry, cursor: RolloutCursor,
             current: RolloutUsage) -> CorrelatedReadings:
        scans.append(now[0])
        return scan_current(cursor, current)

    async def sleep(delay: float) -> None:
        delays.append(delay)
        now[0] += delay
        if len(delays) == 3:
            third_poll.set()
            await appended.wait()

    async def writer() -> None:
        await third_poll.wait()
        _append_token_count(home, raw_usage, 17_447, rate_limits=_LIMITS)
        appended.set()

    monkeypatch.setattr(RolloutTelemetry, "scan_current", scan)
    monkeypatch.setattr(cr, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(cr.asyncio, "sleep", sleep)

    with anyio.fail_after(30):
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(writer)
            readings = await cr._wait_for_current_readings(telemetry, cursor, usage)

    assert readings.context == (17_447, _WINDOW)
    assert readings.quota is not None and readings.quota.raw == _LIMITS
    assert scans == pytest.approx([10.0, 10.025, 10.05, 10.075])
    assert delays == pytest.approx([0.025, 0.025, 0.025])
    assert now[0] == pytest.approx(10.075)


@pytest.mark.anyio
async def test_wait_never_retries_at_or_after_two_second_deadline(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _codex_home(tmp_path, records=[])
    telemetry = RolloutTelemetry(home, "thr-1")
    cursor = telemetry.begin_turn()
    usage = _rollout_usage(_completed_usage())
    now = [10.0]
    scans: list[float] = []
    delays: list[float] = []
    finished: list[tuple[RolloutCursor, RolloutUsage, bool]] = []
    scan_current = telemetry.scan_current
    finish_turn = telemetry.finish_turn

    def scan(_: RolloutTelemetry, current_cursor: RolloutCursor,
             current_usage: RolloutUsage) -> CorrelatedReadings:
        scans.append(now[0])
        result = scan_current(current_cursor, current_usage)
        if len(scans) == 1:
            now[0] += 0.4
        return result

    def finish(_: RolloutTelemetry, current_cursor: RolloutCursor,
               current_usage: RolloutUsage, matched: bool) -> None:
        finished.append((current_cursor, current_usage, matched))
        finish_turn(current_cursor, current_usage, matched)

    async def sleep(delay: float) -> None:
        delays.append(delay)
        now[0] += delay

    monkeypatch.setattr(RolloutTelemetry, "scan_current", scan)
    monkeypatch.setattr(RolloutTelemetry, "finish_turn", finish)
    monkeypatch.setattr(cr.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(cr.asyncio, "sleep", sleep)

    readings = await cr._wait_for_current_readings(telemetry, cursor, usage)

    assert readings == RolloutReadings(context=None, quota=None)
    assert scans[0] == 10.0
    assert scans[1] == pytest.approx(10.425)
    assert len(scans) > 3
    assert all(scan_time < 12.0 for scan_time in scans[1:])
    assert sum(delays) == pytest.approx(1.6)
    assert now[0] == pytest.approx(12.0)
    assert finished == [(cursor, usage, False)]


@pytest.mark.anyio
async def test_tool_turn_matches_cumulative_usage_but_uses_last_occupancy(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _codex_home(tmp_path, records=[])
    telemetry = RolloutTelemetry(home, "thr-1")
    cursor = telemetry.begin_turn()
    usage = _completed_usage()
    _append_token_count(home, _completed_usage(
        input_tokens=17_122, cached_input_tokens=9_984, cache_write_input_tokens=0,
        output_tokens=68, reasoning_output_tokens=21), 17_190)
    _append_token_count(home, usage, 17_447)

    async def no_sleep(_: float) -> None:
        raise AssertionError("matching record was immediately available")

    monkeypatch.setattr(cr.asyncio, "sleep", no_sleep)

    readings = await cr._wait_for_current_readings(telemetry, cursor, _rollout_usage(usage))

    assert readings.context == (17_447, _WINDOW)


def test_quota_free_current_turn_preserves_prior_valid_quota(tmp_path: Path) -> None:
    home = _codex_home(tmp_path, records=[])
    elog = InMemoryEventLog()
    state: dict[str, Any] = {"thread_id": "thr-1", "auth_mode": "chatgpt",
                             "quota_reading": None}
    _drive_turn(elog, state, home, _readings(22_389, rate_limits=_LIMITS))
    prior = state["quota_reading"]
    _drive_turn(elog, state, home, _readings(22_500))

    messages = [event["payload"] for event in elog.read()
                if event["type"] == "agent_message"]
    quota_events = [event for event in elog.read() if event["type"] == "quota_reading"]
    assert state["context_reading"] == (22_500, _WINDOW)
    assert state["quota_reading"] is prior
    assert [message["context_tokens"] for message in messages] == [22_389, 22_500]
    assert len(quota_events) == 1
    assert quota_events[0]["payload"]["plan"] == "plus"
    assert quota_events[0]["payload"]["raw"] is _LIMITS


@pytest.mark.anyio
async def test_unreadable_rollout_is_nonfatal_and_preserves_prior_quota(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _codex_home(tmp_path, records=[])
    telemetry = RolloutTelemetry(home, "thr-1")
    cursor = telemetry.begin_turn()
    usage = _rollout_usage(_completed_usage())
    now = [10.0]
    scans: list[float] = []
    delays: list[float] = []

    def unreadable(_: RolloutTelemetry, current_cursor: RolloutCursor,
                   current_usage: RolloutUsage) -> CorrelatedReadings:
        scans.append(now[0])
        if len(scans) == 1:
            now[0] += 0.4
        raise OSError("rollout vanished")

    async def sleep(delay: float) -> None:
        delays.append(delay)
        now[0] += delay

    monkeypatch.setattr(RolloutTelemetry, "scan_current", unreadable)
    monkeypatch.setattr(cr.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(cr.asyncio, "sleep", sleep)

    readings = await cr._wait_for_current_readings(telemetry, cursor, usage)

    assert readings == RolloutReadings(context=None, quota=None)
    assert len(scans) > 3
    assert all(scan_time < 12.0 for scan_time in scans[1:])
    assert sum(delays) == pytest.approx(1.6)
    assert now[0] == pytest.approx(12.0)

    prior = quota_from_rate_limits(_LIMITS)
    assert prior is not None
    elog = InMemoryEventLog()
    state: dict[str, Any] = {"thread_id": "thr-1", "auth_mode": "chatgpt",
                             "quota_reading": prior}
    process_event(agent_message("done"), elog=elog, agent_id="step:a", state=state,
                  provider="codex", model="gpt-5.6-luna", codex_home=home)
    process_event(turn_completed(input_tokens=34_552, cached_input_tokens=26_112,
                                 cache_write_input_tokens=0, output_tokens=85,
                                 reasoning_output_tokens=29), elog=elog, agent_id="step:a",
                  state=state, provider="codex", model="gpt-5.6-luna", codex_home=home,
                  rollout_readings=readings)

    payload = next(event["payload"] for event in elog.read()
                   if event["type"] == "agent_message")
    warnings = [event["payload"]["message"] for event in elog.read()
                if event["type"] == "runner_warning"]
    assert payload["usage"]["output_tokens"] == 85
    assert "context_tokens" not in payload and "context_window" not in payload
    assert state["context_reading"] is None
    assert state["quota_reading"] is prior
    assert warnings == [_CANARY]


def test_proc_state_seeds_the_canary_latches_and_the_quota_slot() -> None:
    src = inspect.getsource(cr.CodexSession._enter)
    assert '"context_canary_emitted": False' in src
    assert '"compaction_canary_emitted": False' in src
    assert '"quota_reading": None' in src
    assert 'RolloutTelemetry(codex_home, None)' in src
    assert 'RolloutCursor((), discovery_complete=True)' in src

"""Unit tests for codex rollout persist/restore (Fix 1 core mechanics)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.providers.codex_rollout import (
    compaction_seen,
    latest_readings,
    persist_rollout,
    quota_from_rate_limits,
    restore_rollout,
)


def _plant_rollout(codex_home: Path, thread_id: str,
                   body: str = '{"codeword": "MANGO-5512"}\n') -> Path:
    rel = Path("sessions/2026/06/05") / f"rollout-2026-06-05T00-00-00-{thread_id}.jsonl"
    dest = codex_home / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body)
    return dest


def test_persist_copies_rollout_and_writes_thread_json(tmp_path: Path) -> None:
    home = tmp_path / "codex_home"
    home.mkdir()
    _plant_rollout(home, "thr-1")
    tx = tmp_path / "tx"
    tx.mkdir()

    ok = persist_rollout(codex_home=home, thread_id="thr-1", transcript_dir=tx)

    assert ok is True
    meta = json.loads((tx / "codex_thread.json").read_text())
    assert meta["thread_id"] == "thr-1"
    saved = tx / "codex_rollout" / meta["rollout_relpath"]
    assert saved.exists()
    assert "MANGO-5512" in saved.read_text()


def test_persist_returns_false_when_no_rollout(tmp_path: Path) -> None:
    home = tmp_path / "codex_home"
    (home / "sessions").mkdir(parents=True)  # exists but empty
    tx = tmp_path / "tx"
    tx.mkdir()
    assert persist_rollout(codex_home=home, thread_id="thr-1", transcript_dir=tx) is False
    assert not (tx / "codex_thread.json").exists()


def test_restore_round_trips_into_fresh_home_and_returns_thread_id(tmp_path: Path) -> None:
    # Simulate: persist out of home_a, tear it down, restore into fresh home_b.
    home_a = tmp_path / "home_a"
    home_a.mkdir()
    rollout = _plant_rollout(home_a, "thr-1")
    rel = rollout.relative_to(home_a)
    tx = tmp_path / "tx"
    tx.mkdir()
    persist_rollout(codex_home=home_a, thread_id="thr-1", transcript_dir=tx)

    home_b = tmp_path / "home_b"
    home_b.mkdir()
    thread_id = restore_rollout(
        codex_home=home_b, transcript_dir=tx)

    assert thread_id == "thr-1"
    restored = home_b / rel
    assert restored.exists()
    assert "MANGO-5512" in restored.read_text()


def test_restore_fails_loudly_when_thread_json_missing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    tx = tmp_path / "tx"
    tx.mkdir()
    with pytest.raises(FileNotFoundError):
        restore_rollout(
            codex_home=home, transcript_dir=tx)


def test_restore_fails_loudly_when_rollout_bytes_missing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    tx = tmp_path / "tx"
    tx.mkdir()
    # thread.json present but the rollout file was never copied.
    (tx / "codex_thread.json").write_text(
        json.dumps({"thread_id": "thr-1",
                    "rollout_relpath": "sessions/x/rollout-thr-1.jsonl"}))
    with pytest.raises(FileNotFoundError):
        restore_rollout(
            codex_home=home, transcript_dir=tx)


_WINDOW = 258400
_RATE_LIMITS = {
    "limit_id": "codex", "limit_name": None,
    "primary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": 1788403732},
    "secondary": {"used_percent": 14.0, "window_minutes": 10080, "resets_at": 1788971433},
    "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
    "individual_limit": None, "spend_control_reached": None,
    "plan_type": "plus", "rate_limit_reached_type": None,
}


def _token_count_line(*, total_tokens: object = 22389, window: object = _WINDOW,
                      rate_limits: object = None) -> str:
    """One codex `token_count` record, verbatim in shape. The distractor values
    (82956 turn sum, 22290 input_tokens) pin which field the reader must take."""
    return json.dumps({
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {"input_tokens": 82628, "total_tokens": 82956},
                "last_token_usage": {
                    "input_tokens": 22290,
                    "cached_input_tokens": 20992,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 99,
                    "reasoning_output_tokens": 70,
                    "total_tokens": total_tokens,
                },
                "model_context_window": window,
            },
            "rate_limits": rate_limits,
        },
    }) + "\n"


def _home_with(tmp_path: Path, body: str) -> Path:
    home = tmp_path / "codex_home"
    home.mkdir()
    _plant_rollout(home, "thr-1", body)
    return home


def test_latest_readings_returns_occupancy_and_window(tmp_path: Path) -> None:
    home = _home_with(tmp_path, _token_count_line(total_tokens=22389))
    assert latest_readings(home, "thr-1").context == (22389, _WINDOW)


def test_latest_readings_takes_the_last_record(tmp_path: Path) -> None:
    body = "".join(_token_count_line(total_tokens=t)
                   for t in (20115, 22389, 21223))
    home = _home_with(tmp_path, body)
    assert latest_readings(home, "thr-1").context == (21223, _WINDOW)


def test_latest_readings_returns_none_when_rollout_absent(tmp_path: Path) -> None:
    home = tmp_path / "codex_home"
    (home / "sessions").mkdir(parents=True)  # exists but empty
    readings = latest_readings(home, "thr-1")
    assert readings.context is None
    assert readings.quota is None


def test_latest_readings_returns_none_without_token_count_record(
        tmp_path: Path) -> None:
    body = (json.dumps({"type": "session_meta", "payload": {"id": "thr-1"}}) + "\n"
            + json.dumps({"type": "event_msg",
                          "payload": {"type": "agent_message",
                                      "message": "hi"}}) + "\n")
    home = _home_with(tmp_path, body)
    assert latest_readings(home, "thr-1").context is None


@pytest.mark.parametrize("window", [None, 0, -1])
def test_latest_readings_returns_none_for_unusable_window(
        tmp_path: Path, window: object) -> None:
    home = _home_with(tmp_path, _token_count_line(window=window))
    assert latest_readings(home, "thr-1").context is None


@pytest.mark.parametrize("total_tokens", [None, 0, -1])
def test_latest_readings_returns_none_for_unusable_tokens(
        tmp_path: Path, total_tokens: object) -> None:
    home = _home_with(tmp_path, _token_count_line(total_tokens=total_tokens))
    assert latest_readings(home, "thr-1").context is None


def test_latest_readings_skips_a_trailing_unusable_record(tmp_path: Path) -> None:
    # A later drifted record must not erase an earlier complete reading.
    body = (_token_count_line(total_tokens=22389)
            + _token_count_line(total_tokens=22500, window=None))
    home = _home_with(tmp_path, body)
    assert latest_readings(home, "thr-1").context == (22389, _WINDOW)


def test_latest_readings_returns_none_when_usage_missing(tmp_path: Path) -> None:
    body = json.dumps({
        "type": "event_msg",
        "payload": {"type": "token_count",
                    "info": {"model_context_window": _WINDOW}},
    }) + "\n"
    home = _home_with(tmp_path, body)
    assert latest_readings(home, "thr-1").context is None


def test_latest_readings_returns_both_readings_from_one_pass(tmp_path: Path) -> None:
    home = _home_with(tmp_path, _token_count_line(rate_limits=_RATE_LIMITS))
    readings = latest_readings(home, "thr-1")
    assert readings.context == (22389, _WINDOW)
    assert readings.quota is not None
    assert [(w.label, w.used_pct) for w in readings.quota.windows] == [
        ("5h", 12.0), ("7d", 14.0)]


def test_latest_readings_takes_the_last_valid_of_each_independently(
        tmp_path: Path) -> None:
    # `rate_limits` can be null on a record whose token counts are valid: the
    # newer record advances the context reading without erasing the quota one.
    body = (_token_count_line(total_tokens=22389, rate_limits=_RATE_LIMITS)
            + _token_count_line(total_tokens=22500, rate_limits=None))
    home = _home_with(tmp_path, body)
    readings = latest_readings(home, "thr-1")
    assert readings.context == (22500, _WINDOW)
    assert readings.quota is not None
    assert readings.quota.plan == "plus"


def _compacted_line() -> str:
    """codex's real top-level compaction record, shape-verbatim from the spike
    rollouts (`.imp/quests/sila75/artifacts/spike/rollouts/zero.rollout.jsonl`)."""
    return json.dumps({
        "timestamp": "2026-09-04T06:58:53.348Z",
        "ordinal": 18,
        "type": "compacted",
        "payload": {
            "message": "",
            "replacement_history": [
                {"type": "compaction", "id": "cmp_0d8ba4f379",
                 "encrypted_content": "gAAAAABqmmxw"},
            ],
        },
    }) + "\n"


def _decoy_lines() -> str:
    """Lines that must not trip the predicate: a truncated record the pre-filter
    hands to json.loads anyway, and an agent message whose prose says
    "compacted" without being codex's compaction record."""
    return (
        '{"type": "compacted", "payload": {"message": "trunca\n'
        + json.dumps({
            "type": "event_msg",
            "payload": {"type": "agent_message",
                        "message": "the history was compacted upstream"},
        }) + "\n"
    )


def test_compaction_seen_true_for_a_compacted_rollout(tmp_path: Path) -> None:
    body = _decoy_lines() + _token_count_line() + _compacted_line()
    home = _home_with(tmp_path, body)
    assert compaction_seen(home, "thr-1") is True


def test_compaction_seen_false_without_a_compacted_record(tmp_path: Path) -> None:
    # Decoys carry the substring; a bare match with no type check would return True.
    body = _decoy_lines() + _token_count_line()
    home = _home_with(tmp_path, body)
    assert compaction_seen(home, "thr-1") is False


def test_compaction_seen_false_when_rollout_absent(tmp_path: Path) -> None:
    home = tmp_path / "codex_home"
    (home / "sessions").mkdir(parents=True)  # exists but empty
    assert compaction_seen(home, "thr-1") is False


def test_quota_from_rate_limits_labels_each_window_from_its_own_duration() -> None:
    reading = quota_from_rate_limits(_RATE_LIMITS)
    assert reading is not None
    assert reading.provider == "codex"
    assert reading.plan == "plus"
    assert [(w.label, w.used_pct, w.resets_at) for w in reading.windows] == [
        ("5h", 12.0, 1788403732), ("7d", 14.0, 1788971433)]


def test_quota_from_rate_limits_handles_the_pro_shape() -> None:
    # On a `pro` account the WEEKLY window is reported in `primary`, with a null
    # `secondary`: nothing may assume `primary` means 5h.
    block = {"primary": {"used_percent": 31.0, "window_minutes": 10080,
                         "resets_at": 1788971433},
             "secondary": None, "plan_type": "pro"}
    reading = quota_from_rate_limits(block)
    assert reading is not None
    assert reading.plan == "pro"
    assert [(w.label, w.used_pct) for w in reading.windows] == [("7d", 31.0)]


def test_quota_from_rate_limits_orders_by_duration_not_position() -> None:
    block = {"primary": {"used_percent": 5, "window_minutes": 10080, "resets_at": 2},
             "secondary": {"used_percent": 9, "window_minutes": 300, "resets_at": 1},
             "plan_type": "plus"}
    reading = quota_from_rate_limits(block)
    assert reading is not None
    assert [(w.label, w.used_pct) for w in reading.windows] == [("5h", 9.0), ("7d", 5.0)]
    assert isinstance(reading.windows[0].used_pct, float)


def test_quota_from_rate_limits_labels_a_day_long_window_in_days() -> None:
    block = {"primary": {"used_percent": 4.0, "window_minutes": 1440, "resets_at": None}}
    reading = quota_from_rate_limits(block)
    assert reading is not None
    assert [w.label for w in reading.windows] == ["1d"]
    assert reading.windows[0].resets_at is None
    assert reading.plan is None


def test_quota_from_rate_limits_carries_the_block_verbatim() -> None:
    reading = quota_from_rate_limits(_RATE_LIMITS)
    assert reading is not None
    assert reading.raw == _RATE_LIMITS
    assert reading.raw["credits"] == {"has_credits": False, "unlimited": False,
                                      "balance": "0"}
    assert reading.raw["rate_limit_reached_type"] is None


@pytest.mark.parametrize("block", [
    None,
    {},
    {"primary": None, "secondary": None, "plan_type": "plus"},
    {"primary": {"used_percent": 3.0}},                 # no window_minutes
    {"primary": {"window_minutes": 300}},               # no used_percent
    {"primary": {"used_percent": "high", "window_minutes": 300}},
    {"primary": {"used_percent": 3.0, "window_minutes": 0}},  # zero rejected
])
def test_quota_from_rate_limits_without_a_usable_window_is_none(block: object) -> None:
    assert quota_from_rate_limits(block) is None

"""Unit tests for codex rollout persist/restore (Fix 1 core mechanics)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.providers.codex_rollout import (
    latest_context_reading,
    persist_rollout,
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


def _token_count_line(*, total_tokens: object = 22389, window: object = _WINDOW) -> str:
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
            "rate_limits": {},
        },
    }) + "\n"


def _home_with(tmp_path: Path, body: str) -> Path:
    home = tmp_path / "codex_home"
    home.mkdir()
    _plant_rollout(home, "thr-1", body)
    return home


def test_latest_context_reading_returns_occupancy_and_window(tmp_path: Path) -> None:
    home = _home_with(tmp_path, _token_count_line(total_tokens=22389))
    assert latest_context_reading(home, "thr-1") == (22389, _WINDOW)


def test_latest_context_reading_takes_the_last_record(tmp_path: Path) -> None:
    body = "".join(_token_count_line(total_tokens=t)
                   for t in (20115, 22389, 21223))
    home = _home_with(tmp_path, body)
    assert latest_context_reading(home, "thr-1") == (21223, _WINDOW)


def test_latest_context_reading_returns_none_when_rollout_absent(tmp_path: Path) -> None:
    home = tmp_path / "codex_home"
    (home / "sessions").mkdir(parents=True)  # exists but empty
    assert latest_context_reading(home, "thr-1") is None


def test_latest_context_reading_returns_none_without_token_count_record(
        tmp_path: Path) -> None:
    body = (json.dumps({"type": "session_meta", "payload": {"id": "thr-1"}}) + "\n"
            + json.dumps({"type": "event_msg",
                          "payload": {"type": "agent_message",
                                      "message": "hi"}}) + "\n")
    home = _home_with(tmp_path, body)
    assert latest_context_reading(home, "thr-1") is None


@pytest.mark.parametrize("window", [None, 0, -1])
def test_latest_context_reading_returns_none_for_unusable_window(
        tmp_path: Path, window: object) -> None:
    home = _home_with(tmp_path, _token_count_line(window=window))
    assert latest_context_reading(home, "thr-1") is None


@pytest.mark.parametrize("total_tokens", [None, 0, -1])
def test_latest_context_reading_returns_none_for_unusable_tokens(
        tmp_path: Path, total_tokens: object) -> None:
    home = _home_with(tmp_path, _token_count_line(total_tokens=total_tokens))
    assert latest_context_reading(home, "thr-1") is None


def test_latest_context_reading_skips_a_trailing_unusable_record(
        tmp_path: Path) -> None:
    # A later drifted record must not erase an earlier complete reading.
    body = (_token_count_line(total_tokens=22389)
            + _token_count_line(total_tokens=22500, window=None))
    home = _home_with(tmp_path, body)
    assert latest_context_reading(home, "thr-1") == (22389, _WINDOW)


def test_latest_context_reading_returns_none_when_usage_missing(tmp_path: Path) -> None:
    body = json.dumps({
        "type": "event_msg",
        "payload": {"type": "token_count",
                    "info": {"model_context_window": _WINDOW}},
    }) + "\n"
    home = _home_with(tmp_path, body)
    assert latest_context_reading(home, "thr-1") is None

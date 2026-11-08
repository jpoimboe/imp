"""The quota extract cache: a disposable, rebuildable copy of each quest's raw
quota facts."""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from imp.cli import quota_cache
from imp.cli.quota_cache import cache_dir, cached_extract
from imp.cli.quota_share import extract

_EVENTS: list[dict[str, Any]] = [
    {"type": "agent_message", "agent_id": "step:a",
     "timestamp": "2026-06-01T04:00:00+00:00",
     "payload": {"provider": "claude", "model": "claude-sonnet-4-6",
                 "auth_mode": "subscription"}},
    {"type": "query_result", "agent_id": "step:a",
     "timestamp": "2026-06-01T04:01:00.250000+00:00",
     "payload": {"session_id": "s1", "duration_ms": 30000,
                 "model_usage": {"claude-sonnet-4-6": {
                     "inputTokens": 1200, "outputTokens": 340,
                     "cacheReadInputTokens": 9000,
                     "cacheCreationInputTokens": 50}}}},
    {"type": "agent_message", "agent_id": "step:b",
     "timestamp": "2026-06-01T04:02:00+00:00",
     "payload": {"provider": "codex", "model": "gpt-5.3-codex",
                 "auth_mode": "subscription", "usage": {}}},
    {"type": "agent_message_usage_patch", "agent_id": "step:b",
     "payload": {"usage": {"input_tokens": 1000, "cache_read_input_tokens": 200,
                           "output_tokens": 50}}},
    {"type": "quota_reading", "agent_id": "step:a",
     "timestamp": "2026-06-01T04:03:00+00:00",
     "payload": {"provider": "claude",
                 "windows": [{"label": "7d", "used_pct": 12.5,
                              "resets_at": 1780642800}]}},
]


def _raw(events: list[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(e) + "\n" for e in events).encode()


class _Reader:
    """A log read that counts how often the cache fell through to it."""

    def __init__(self, events: list[dict[str, Any]] | None = _EVENTS) -> None:
        self.raw = None if events is None else _raw(events)
        self.calls = 0

    def __call__(self) -> bytes | None:
        self.calls += 1
        return self.raw


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))


def test_a_warm_entry_returns_the_same_extract_without_reading_the_log(
    tmp_path: Path,
) -> None:
    fresh = extract(_EVENTS)
    # one interval increment, one point increment, one reading: every field kind
    assert sorted(i.start is None for i in fresh.increments) == [False, True]
    assert len(fresh.readings) == 1
    reader = _Reader()
    cold = cached_extract("proj", "q1", ["live", 10, 20], reader)
    warm = cached_extract("proj", "q1", ["live", 10, 20], reader)
    assert cold == fresh
    assert warm == fresh
    assert reader.calls == 1
    (entry,) = (tmp_path / "imphome" / "cache" / "quota").glob("*.json")
    assert entry.parent == cache_dir()
    assert list(cache_dir().iterdir()) == [entry]


def test_a_changed_key_rereads_the_log() -> None:
    cached_extract("proj", "q1", ["live", 10, 20], _Reader())
    grown = [*_EVENTS, {"type": "quota_reading", "agent_id": "step:a",
                        "timestamp": "2026-06-01T04:09:00+00:00",
                        "payload": {"provider": "claude",
                                    "windows": [{"label": "7d", "used_pct": 14.0,
                                                 "resets_at": 1780642800}]}}]
    reader = _Reader(grown)
    assert cached_extract("proj", "q1", ["live", 11, 20], reader) == extract(grown)
    assert reader.calls == 1


def test_entries_are_kept_per_project_and_quest() -> None:
    cached_extract("p1", "q1", ["blob", "abc"], _Reader())
    other = _EVENTS[:2]
    reader = _Reader(other)
    assert cached_extract("p2", "q1", ["blob", "abc"], reader) == extract(other)
    assert reader.calls == 1
    assert len(list(cache_dir().glob("*.json"))) == 2


def test_an_entry_from_another_extract_version_is_recomputed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 9(g)."""
    cached_extract("proj", "q1", ["blob", "abc"], _Reader())
    monkeypatch.setattr(quota_cache, "EXTRACT_VERSION", quota_cache.EXTRACT_VERSION + 1)
    reader = _Reader()
    assert cached_extract("proj", "q1", ["blob", "abc"], reader) == extract(_EVENTS)
    assert reader.calls == 1


_DAMAGE: list[tuple[str, Callable[[bytes], bytes | None]]] = [
    ("deleted", lambda valid: None),
    ("empty", lambda valid: b""),
    ("truncated", lambda valid: valid[: len(valid) // 2]),
    ("not-json", lambda valid: b"{not json"),
    ("a-list", lambda valid: b"[]"),
    ("no-increments",
     lambda valid: json.dumps({"key": json.loads(valid)["key"], "readings": []}).encode()),
    ("bad-billing", lambda valid: valid.replace(b'"output"', b'"outptu"')),
]


@pytest.mark.parametrize("damage", [d for _n, d in _DAMAGE], ids=[n for n, _d in _DAMAGE])
def test_a_damaged_entry_is_rebuilt_from_the_log(
    damage: Callable[[bytes], bytes | None],
) -> None:
    """AC 9(c)."""
    cached_extract("proj", "q1", ["blob", "abc"], _Reader())
    (entry,) = cache_dir().glob("*.json")
    broken = damage(entry.read_bytes())
    if broken is None:
        entry.unlink()
    else:
        entry.write_bytes(broken)
    reader = _Reader()
    assert cached_extract("proj", "q1", ["blob", "abc"], reader) == extract(_EVENTS)
    assert reader.calls == 1
    again = _Reader()
    assert cached_extract("proj", "q1", ["blob", "abc"], again) == extract(_EVENTS)
    assert again.calls == 0


def test_an_unwritable_cache_still_answers(tmp_path: Path) -> None:
    (tmp_path / "imphome").mkdir()
    (tmp_path / "imphome" / "cache").write_text("a file where a directory belongs\n")
    reader = _Reader()
    assert cached_extract("proj", "q1", ["blob", "abc"], reader) == extract(_EVENTS)
    assert cached_extract("proj", "q1", ["blob", "abc"], reader) == extract(_EVENTS)
    assert reader.calls == 2


def test_a_missing_log_is_none_and_caches_nothing() -> None:
    assert cached_extract("proj", "q1", ["blob", "gone"], _Reader(None)) is None
    assert not cache_dir().exists()

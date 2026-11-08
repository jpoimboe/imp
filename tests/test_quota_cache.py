"""The quota extract cache: a disposable, rebuildable copy of each quest's raw
quota facts."""
from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from imp.cli import quota_cache
from imp.cli.quota_cache import cache_dir, cached_extract
from imp.cli.quota_scan import scan_weeks
from imp.cli.quota_share import extract
from imp.cli.store import GaugeWeek
from imp.engine.archive.blobstore import LocalBlobStore
from imp.providers.catalog import MODEL_PRICES, ModelPrices
from tests.conftest import _archive_with_event
from tests.test_quota_scan import (
    _RESET,
    _imp_home,
    _jsonl,
    _live_home,
    _msg,
    _project,
    _qr,
    _reading,
    _subject_project,
)

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


_SUBJECT = [_msg("2026-06-01T03:59:00+00:00"),
            _qr("2026-06-01T04:01:00+00:00", tokens=1_000_000)]


@pytest.fixture
def parsed(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The byte length of every log the cache fell through to and parsed."""
    calls: list[int] = []
    real = quota_cache._parse_jsonl

    def counting(raw: bytes) -> list[dict[str, Any]]:
        calls.append(len(raw))
        return real(raw)

    monkeypatch.setattr(quota_cache, "_parse_jsonl", counting)
    return calls


@pytest.fixture
def blob_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []
    real = LocalBlobStore.get

    def get(self: LocalBlobStore, digest: str) -> bytes | None:
        reads.append(digest)
        return real(self, digest)

    monkeypatch.setattr(LocalBlobStore, "get", get)
    return reads


def _world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The subject's project and two others. liveq (3.0 units and a 30-point
    reading) is live in `elsewhere`. archq (1.5) is archived in `other`, and
    bothq (6.0) is both live and archived there."""
    home = _imp_home(tmp_path, monkeypatch)
    root = _subject_project(tmp_path, home)
    _live_home(home / "projects" / "subject", "samo71", _SUBJECT)
    _live_home(_project(home, "elsewhere"), "liveq",
               [_msg("2026-06-01T04:09:00+00:00"),
                _qr("2026-06-01T04:10:00+00:00", tokens=200_000),
                _reading("2026-06-01T04:11:00+00:00", 30.0)])
    other = tmp_path / "other"
    other.mkdir()
    _archive_with_event(other, "archq", "Archived", _jsonl(
        [_msg("2026-06-01T04:19:00+00:00"),
         _qr("2026-06-01T04:20:00+00:00", tokens=100_000)]))
    both = [_msg("2026-06-01T04:29:00+00:00"),
            _qr("2026-06-01T04:30:00+00:00", tokens=400_000)]
    _archive_with_event(other, "bothq", "Both", _jsonl(both))
    _live_home(home / "projects" / "other", "bothq", both)
    return root


def _figures(weeks: list[GaugeWeek]) -> list[tuple[str, int, float, float]]:
    return [(w.provider, w.resets_at, w.peak_pct, round(w.other_units, 6)) for w in weeks]


def test_a_warm_scan_reads_no_other_quests_log_or_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    parsed: list[int], blob_reads: list[str],
) -> None:
    """AC 9(a). The cold scan already reads one blob, archq's: bothq's live home
    shadows its archived copy, and the subject is never read."""
    root = _world(tmp_path, monkeypatch)
    parsed.clear()
    blob_reads.clear()
    cold = scan_weeks(root, "samo71", _SUBJECT)
    assert _figures(cold) == [("claude", _RESET, 30.0, 10.5)]
    assert (len(parsed), len(blob_reads)) == (3, 1)
    assert len(list(cache_dir().glob("*.json"))) == 3
    parsed.clear()
    blob_reads.clear()
    assert scan_weeks(root, "samo71", _SUBJECT) == cold
    assert (parsed, blob_reads) == ([], [])


def test_a_live_log_that_changes_is_reread_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parsed: list[int],
) -> None:
    """AC 9(d): a size change and an mtime-only change each re-read that log and
    no other."""
    root = _world(tmp_path, monkeypatch)
    scan_weeks(root, "samo71", _SUBJECT)
    events = tmp_path / "imphome" / "projects" / "elsewhere" / "quests" / "liveq" / "events.jsonl"
    with events.open("a") as fh:
        fh.write(_jsonl([_reading("2026-06-01T04:12:00+00:00", 45.0)]))
    parsed.clear()
    assert _figures(scan_weeks(root, "samo71", _SUBJECT)) == [("claude", _RESET, 45.0, 10.5)]
    assert parsed == [events.stat().st_size]
    st = events.stat()
    os.utime(events, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    parsed.clear()
    scan_weeks(root, "samo71", _SUBJECT)
    assert parsed == [st.st_size]


def test_a_new_anchor_from_another_quest_reaches_a_warm_neighbour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parsed: list[int],
) -> None:
    """AC 9(e): unitq's 3.0 units sit in its warm entry with no week to fall in.
    When anchorq gains a reading, those units join the new week, and only
    anchorq's log is re-read."""
    home = _imp_home(tmp_path, monkeypatch)
    root = _subject_project(tmp_path, home)
    elsewhere = _project(home, "elsewhere")
    _live_home(elsewhere, "unitq", [_msg("2026-06-01T04:09:00+00:00"),
                                    _qr("2026-06-01T04:10:00+00:00", tokens=200_000)])
    anchor = _live_home(elsewhere, "anchorq", [_msg("2026-06-01T04:20:00+00:00")])
    assert scan_weeks(root, "samo71", _SUBJECT) == []
    with (anchor / "events.jsonl").open("a") as fh:
        fh.write(_jsonl([_reading("2026-06-01T04:21:00+00:00", 40.0)]))
    parsed.clear()
    warm = scan_weeks(root, "samo71", _SUBJECT)
    assert _figures(warm) == [("claude", _RESET, 40.0, 3.0)]
    assert parsed == [(anchor / "events.jsonl").stat().st_size]
    shutil.rmtree(cache_dir())
    assert scan_weeks(root, "samo71", _SUBJECT) == warm


def test_a_price_change_reaches_warm_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parsed: list[int],
) -> None:
    """AC 9(f). Sonnet's output rate doubles, so liveq, archq and bothq go from
    10.5 to 21.0 units. newq's model was unpriced (0 units) and now prices
    200k output at $15/M (3.0). None of the four logs is re-read."""
    root = _world(tmp_path, monkeypatch)
    msg = _msg("2026-06-01T04:39:00+00:00")
    msg["payload"]["model"] = "claude-imaginary-9"
    qr = _qr("2026-06-01T04:40:00+00:00")
    qr["payload"]["model_usage"] = {"claude-imaginary-9": {"outputTokens": 200_000}}
    _live_home(tmp_path / "imphome" / "projects" / "elsewhere", "newq", [msg, qr])
    assert _figures(scan_weeks(root, "samo71", _SUBJECT)) == [("claude", _RESET, 30.0, 10.5)]
    monkeypatch.setitem(MODEL_PRICES, "claude-sonnet-4-6", ModelPrices(3.00, 30.00, 0.30, 6.00))
    monkeypatch.setitem(MODEL_PRICES, "claude-imaginary-9", ModelPrices(3.00, 15.00, 0.30, 6.00))
    parsed.clear()
    warm = scan_weeks(root, "samo71", _SUBJECT)
    assert _figures(warm) == [("claude", _RESET, 30.0, 24.0)]
    assert parsed == []
    shutil.rmtree(cache_dir())
    assert scan_weeks(root, "samo71", _SUBJECT) == warm


def test_a_scan_after_an_extract_version_bump_rereads_every_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parsed: list[int],
) -> None:
    """AC 9(g)."""
    root = _world(tmp_path, monkeypatch)
    cold = scan_weeks(root, "samo71", _SUBJECT)
    monkeypatch.setattr(quota_cache, "EXTRACT_VERSION", quota_cache.EXTRACT_VERSION + 1)
    parsed.clear()
    assert scan_weeks(root, "samo71", _SUBJECT) == cold
    assert len(parsed) == 3

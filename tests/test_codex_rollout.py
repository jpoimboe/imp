"""Unit tests for codex rollout persist/restore (Fix 1 core mechanics)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.providers.codex_rollout import persist_rollout, restore_rollout


def _plant_rollout(codex_home: Path, thread_id: str) -> Path:
    rel = Path("sessions/2026/06/05") / f"rollout-2026-06-05T00-00-00-{thread_id}.jsonl"
    dest = codex_home / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text('{"codeword": "MANGO-5512"}\n')
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
        codex_home=home_b, transcript_dir=tx, events_path=tmp_path / "events.jsonl")

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
            codex_home=home, transcript_dir=tx, events_path=tmp_path / "events.jsonl")


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
            codex_home=home, transcript_dir=tx, events_path=tmp_path / "events.jsonl")


_SAMPLE = (Path(__file__).resolve().parents[1]
           / "docs/superpowers/spikes/codex/codex-rollout-sample.jsonl")


def _plant_sample(codex_home: Path, thread_id: str) -> Path:
    rel = Path("sessions/2026/06/28") / f"rollout-2026-06-28T00-00-00-{thread_id}.jsonl"
    dest = codex_home / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(_SAMPLE.read_text())
    return dest


def _restored_results(codex_home: Path, tx: Path) -> dict[str, str]:
    meta = json.loads((tx / "codex_thread.json").read_text())
    dest = codex_home / meta["rollout_relpath"]
    out: dict[str, str] = {}
    for ln in dest.read_text().splitlines():
        o = json.loads(ln)
        p = o.get("payload") or {}
        if p.get("type") == "function_call_output":
            out[p["call_id"]] = p["output"]
    return out


def test_restore_cold_evicts_old_compactable_outputs(tmp_path: Path) -> None:
    home_a = tmp_path / "home_a"
    home_a.mkdir()
    _plant_sample(home_a, "thr-1")
    tx = tmp_path / "tx"
    tx.mkdir()
    persist_rollout(codex_home=home_a, thread_id="thr-1", transcript_dir=tx)
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps({"type": "llm_response",
                    "timestamp": "2000-01-01T00:00:00+00:00"}) + "\n")

    home_b = tmp_path / "home_b"
    home_b.mkdir()
    restore_rollout(codex_home=home_b, transcript_dir=tx, events_path=events)

    results = _restored_results(home_b, tx)
    assert results["call_a"] == "[old tool result cleared]"   # old read_file
    assert results["call_b"] == "FULL BODY B — patch applied"  # edit_file kept
    assert results["call_c"] == "FULL BODY C — grep hits"      # recent grep kept


def test_restore_warm_keeps_full_outputs_byte_identical(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    home_a = tmp_path / "home_a"
    home_a.mkdir()
    _plant_sample(home_a, "thr-1")
    tx = tmp_path / "tx"
    tx.mkdir()
    persist_rollout(codex_home=home_a, thread_id="thr-1", transcript_dir=tx)
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps({"type": "llm_response",
                    "timestamp": datetime.now(UTC).isoformat()}) + "\n")

    home_b = tmp_path / "home_b"
    home_b.mkdir()
    restore_rollout(codex_home=home_b, transcript_dir=tx, events_path=events)

    meta = json.loads((tx / "codex_thread.json").read_text())
    restored = (home_b / meta["rollout_relpath"]).read_text()
    assert restored == _SAMPLE.read_text()   # warm -> byte-identical

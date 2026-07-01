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

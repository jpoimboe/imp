"""Transient read-path fault recovery in State.load.

Forensics (docs: state.json byte-0 head-truncation) found `imp resume` crashing
at the post-merge exit-code read with a state.json that was byte-identical to the
valid on-disk file MINUS its first byte (`content[1:]`, len N-1) — while the file
was valid on disk before and after, mtime unchanged. The save path is atomic and
the sole writer, so a parse failure on an intact-on-disk file is a transient
read-path fault, not real corruption. State.load re-reads (forcing a cold extent
read) and recovers if a re-read parses, leaving forensic evidence.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.engine import state as state_mod
from imp.engine.errors import StateDecodeError
from imp.engine.state import State

_VALID = json.dumps({
    "id": "c1", "step": "coder", "step_attempt": 0, "status": "new",
    "vars": {}, "loop_counts": {}, "current_step": None, "retry_feedback": None,
}, indent=2, sort_keys=True).encode("utf-8")


def test_transient_head_drop_first_read_recovers(tmp_path: Path, monkeypatch) -> None:
    # On-disk file is VALID; only the first read returns the head-dropped bytes.
    (tmp_path / "state.json").write_bytes(_VALID)
    real = state_mod._read_state_bytes
    calls = {"n": 0}

    def flaky(path: Path) -> bytes:
        calls["n"] += 1
        if calls["n"] == 1:
            return _VALID[1:]          # the transient one-byte head-drop
        return real(path)

    monkeypatch.setattr(state_mod, "_read_state_bytes", flaky)
    loaded = State.load(tmp_path)        # must NOT raise — re-read recovers
    assert loaded.id == "c1"
    assert loaded.status == "new"
    assert calls["n"] >= 2               # at least one retry happened


def test_transient_recovery_leaves_forensic_evidence(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "state.json").write_bytes(_VALID)
    real = state_mod._read_state_bytes
    calls = {"n": 0}

    def flaky(path: Path) -> bytes:
        calls["n"] += 1
        return _VALID[1:] if calls["n"] == 1 else real(path)

    monkeypatch.setattr(state_mod, "_read_state_bytes", flaky)
    State.load(tmp_path)
    # The bad bytes are preserved byte-identically...
    corrupt = list(tmp_path.glob("state.json.corrupt-*"))
    assert len(corrupt) == 1
    assert corrupt[0].read_bytes() == _VALID[1:]
    # ...and a note fingerprints the fault as a head-drop.
    notes = list(tmp_path.glob("state.json.transient-*.json"))
    assert len(notes) == 1
    note = json.loads(notes[0].read_text())
    assert note["head_drop"] is True
    assert note["bad_len"] == len(_VALID) - 1
    assert note["good_len"] == len(_VALID)
    assert note["recovered_on_attempt"] >= 1


def test_persistent_corruption_still_raises_after_retries(tmp_path: Path) -> None:
    # On-disk bytes are genuinely corrupt: every re-read returns the same bad
    # bytes, so retries cannot recover and StateDecodeError is raised as before.
    torn = b'{"id": "c1", "status": "new"}\n{"trailing": true}\n'
    (tmp_path / "state.json").write_bytes(torn)
    with pytest.raises(StateDecodeError):
        State.load(tmp_path)
    # Exactly one sidecar, and NO transient-recovered marker (it never recovered).
    assert len(list(tmp_path.glob("state.json.corrupt-*"))) == 1
    assert list(tmp_path.glob("state.json.transient-*")) == []

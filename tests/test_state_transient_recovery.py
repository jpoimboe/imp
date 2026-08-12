"""Transient read-path fault recovery in State.load.

Forensics (docs: state.json byte-0 head-truncation) found `imp resume` crashing
at the post-merge exit-code read with a state.json that was byte-identical to the
valid on-disk file MINUS its first byte (`content[1:]`, len N-1) — while the file
was valid on disk before and after, mtime unchanged. The save path is atomic and
the sole writer, so a parse failure on an intact-on-disk file is a transient
read-path fault, not real corruption. State.load re-reads (forcing a cold extent
read) and recovers if a re-read parses, recording the fault via imp.engine.readfault.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from imp.engine import readfault
from imp.engine import state as state_mod
from imp.engine.state import InMemoryEventLog, State
from tests._dac import dac_enforced
from tests._faultlog import diag, fault_lines

_VALID = json.dumps({
    "id": "c1", "step": "coder", "step_attempt": 0, "status": "new",
    "vars": {}, "loop_counts": {}, "current_step": None, "retry_feedback": None,
}, indent=2, sort_keys=True).encode("utf-8")

_BAD = _VALID[1:]          # the transient one-byte head-drop


def _head_drop_once(monkeypatch) -> dict[str, int]:
    """Make only the NEXT state.json read return head-dropped bytes."""
    real = state_mod._read_state_bytes
    calls = {"n": 0}

    def flaky(path: Path) -> bytes:
        calls["n"] += 1
        return _BAD if calls["n"] == 1 else real(path)

    monkeypatch.setattr(state_mod, "_read_state_bytes", flaky)
    return calls


def test_transient_head_drop_first_read_recovers(tmp_path: Path, monkeypatch) -> None:
    # On-disk file is VALID; only the first read returns the head-dropped bytes.
    (tmp_path / "state.json").write_bytes(_VALID)
    calls = _head_drop_once(monkeypatch)
    loaded = State.load(tmp_path)        # must NOT raise — re-read recovers
    assert loaded.id == "c1"
    assert loaded.status == "new"
    assert calls["n"] >= 2               # at least one retry happened


def test_recovered_fault_appends_one_fingerprinted_line(
        tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "state.json").write_bytes(_VALID)
    _head_drop_once(monkeypatch)
    State.load(tmp_path)
    lines = fault_lines()
    assert len(lines) == 1
    line = lines[0]
    assert line["site"] == "state"
    assert line["path"] == str(tmp_path / "state.json")
    assert line["recovered"] is True
    assert line["attempt"] >= 1
    assert line["head_drop"] is True
    assert line["tail_drop"] is False
    assert line["bad_len"] == len(_VALID) - 1
    assert line["good_len"] == len(_VALID)
    assert line["bad_sha1"] == hashlib.sha1(_BAD).hexdigest()
    assert line["good_sha1"] == hashlib.sha1(_VALID).hexdigest()
    # the stdlib decode error the head-drop produced, verbatim
    assert line["reason"].startswith("Extra data: line 2")


def test_recovered_fault_preserves_bytes_and_leaves_the_state_dir_clean(
        tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "state.json").write_bytes(_VALID)
    _head_drop_once(monkeypatch)
    State.load(tmp_path)
    blob = diag() / fault_lines()[0]["bytes"]
    assert blob.read_bytes() == _BAD
    # Nothing is written beside state.json any more.
    assert list(tmp_path.glob("state.json.*")) == []
    assert (tmp_path / "state.json").read_bytes() == _VALID


def test_two_recoveries_in_one_process_land_in_distinct_blobs(
        tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "state.json").write_bytes(_VALID)
    real = state_mod._read_state_bytes
    calls = {"n": 0}

    def flaky(path: Path) -> bytes:
        calls["n"] += 1
        # Two loads, each a bad first read (1, 3) then a clean re-read (2, 4).
        return _BAD if calls["n"] in (1, 3) else real(path)

    monkeypatch.setattr(state_mod, "_read_state_bytes", flaky)
    assert State.load(tmp_path).id == "c1"
    assert State.load(tmp_path).id == "c1"
    lines = fault_lines()
    assert len(lines) == 2
    assert lines[0]["bytes"] != lines[1]["bytes"]
    for line in lines:
        assert (diag() / line["bytes"]).read_bytes() == _BAD


def test_recovery_under_a_sink_emits_the_runner_warning(
        tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "state.json").write_bytes(_VALID)
    _head_drop_once(monkeypatch)
    log = InMemoryEventLog()
    with readfault.event_sink(log):
        State.load(tmp_path)
    warnings = [e for e in log.read() if e["type"] == "runner_warning"]
    assert len(warnings) == 1
    message = warnings[0]["payload"]["message"]
    assert message.startswith(
        "transient read fault recovered — state.json re-read cleanly on attempt ")
    assert message.endswith("(head-drop)")
    assert str(tmp_path / "state.json") in warnings[0]["payload"]["detail"]


def test_unwritable_diagnostics_dir_still_recovers(
        tmp_path: Path, monkeypatch) -> None:
    diag().mkdir(parents=True)
    diag().chmod(0o500)                 # readable+executable, not writable
    (tmp_path / "state.json").write_bytes(_VALID)
    _head_drop_once(monkeypatch)
    try:
        with dac_enforced():
            loaded = State.load(tmp_path)    # telemetry failure must be inert
    finally:
        diag().chmod(0o700)
    assert loaded.id == "c1"
    assert not (diag() / "read-faults.jsonl").exists()


def test_a_raising_sink_does_not_break_the_load_or_the_file_record(
        tmp_path: Path, monkeypatch) -> None:
    class _Boom:
        def append(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("sink down")

    (tmp_path / "state.json").write_bytes(_VALID)
    _head_drop_once(monkeypatch)
    with readfault.event_sink(_Boom()):      # type: ignore[arg-type]
        loaded = State.load(tmp_path)
    assert loaded.id == "c1"
    assert len(fault_lines()) == 1

from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.engine.errors import LoadError, StateDecodeError
from imp.engine.state import State
from tests._dac import dac_enforced


_BLOB = Path("/home/u/.imp/diagnostics/bytes/20260811T180311Z-4127-state-1.bad")


def test_state_decode_error_subclasses_loaderror() -> None:
    exc = StateDecodeError(
        live=Path("/ctl/.imp/quests/abc/state/state.json"),
        evidence=_BLOB,
        reason="Extra data: line 2 column 1 (char 31)",
    )
    assert isinstance(exc, LoadError)
    assert exc.evidence == _BLOB
    assert not hasattr(exc, "sidecar")      # hard cutover: no alias left


def test_message_names_live_evidence_and_reason() -> None:
    msg = str(StateDecodeError(
        live=Path("/ctl/.imp/quests/abc/state/state.json"),
        evidence=_BLOB,
        reason="Extra data: line 2 column 1 (char 31)",
    ))
    assert "/ctl/.imp/quests/abc/state/state.json" in msg            # live file
    assert str(_BLOB) in msg                                         # preserved bytes
    assert "Extra data: line 2 column 1 (char 31)" in msg            # stdlib reason
    assert not msg.startswith("imp:")                                # caller adds prefix


def test_message_notes_evidence_unavailable_when_none() -> None:
    msg = str(StateDecodeError(
        live=Path("/ctl/.imp/quests/abc/state/state.json"),
        evidence=None,
        reason="Extra data: line 2 column 1 (char 31)",
    ))
    assert "/ctl/.imp/quests/abc/state/state.json" in msg
    assert "Extra data: line 2 column 1 (char 31)" in msg
    assert "  corrupt bytes could NOT be preserved (evidence write failed)" in msg


# A complete object on line 1 followed by a second object on line 2 — the
# "Extra data: line 2" torn-write signature from the design forensics.
_TORN = b'{"id": "c1", "step": null, "step_attempt": 0, "status": "new"}\n{"trailing": true}\n'


def test_corrupt_state_raises_state_decode_error(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_bytes(_TORN)
    with pytest.raises(StateDecodeError) as ei:
        State.load(tmp_path)
    assert "Extra data: line 2" in ei.value.reason


def test_corrupt_state_writes_byte_identical_sidecar(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_bytes(_TORN)
    with pytest.raises(StateDecodeError) as ei:
        State.load(tmp_path)
    sidecars = list(tmp_path.glob("state.json.corrupt-*"))
    assert len(sidecars) == 1
    assert sidecars[0].read_bytes() == _TORN
    assert ei.value.evidence == sidecars[0]


def test_corrupt_state_message_names_both_paths(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_bytes(_TORN)
    with pytest.raises(StateDecodeError) as ei:
        State.load(tmp_path)
    msg = str(ei.value)
    assert str(tmp_path / "state.json") in msg
    assert str(ei.value.evidence) in msg
    assert "Extra data: line 2" in msg


def test_invalid_utf8_state_raises_with_sidecar(tmp_path: Path) -> None:
    # Bytes that are not valid UTF-8 (invalid start bytes) must raise
    # StateDecodeError, not escape as a bare UnicodeDecodeError, and still
    # preserve byte-identical evidence.
    bad = b"\xff\xfe not utf-8"
    (tmp_path / "state.json").write_bytes(bad)
    with pytest.raises(StateDecodeError):
        State.load(tmp_path)
    sidecars = list(tmp_path.glob("state.json.corrupt-*"))
    assert len(sidecars) == 1
    assert sidecars[0].read_bytes() == bad


def test_readonly_state_dir_swallows_sidecar_write(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_bytes(_TORN)
    tmp_path.chmod(0o500)  # read+exec only: state.json still readable, dir not writable
    try:
        with dac_enforced(), pytest.raises(StateDecodeError) as ei:
            State.load(tmp_path)
    finally:
        tmp_path.chmod(0o700)  # restore so tmp_path cleanup can run
    assert ei.value.evidence is None
    assert list(tmp_path.glob("state.json.corrupt-*")) == []
    assert "Extra data: line 2" in ei.value.reason


def test_happy_path_still_loads_and_coerces_legacy_status(tmp_path: Path) -> None:
    # A valid state.json with the legacy "running" status loads and coerces to
    # "new" exactly as before the rewrite; no sidecar is written.
    (tmp_path / "state.json").write_text(json.dumps({
        "id": "c1", "step": "coder", "step_attempt": 0, "status": "running",
        "vars": {}, "loop_counts": {}, "current_step": None,
        "retry_feedback": None,
    }))
    loaded = State.load(tmp_path)
    assert loaded.status == "new"
    assert loaded.id == "c1"
    assert list(tmp_path.glob("state.json.corrupt-*")) == []

from __future__ import annotations

from pathlib import Path

from imp.engine.errors import LoadError, StateDecodeError


def test_state_decode_error_subclasses_loaderror() -> None:
    exc = StateDecodeError(
        live=Path("/ctl/.imp/changes/abc/state/state.json"),
        sidecar=Path("/ctl/.imp/changes/abc/state/state.json.corrupt-X-1"),
        reason="Extra data: line 2 column 1 (char 31)",
    )
    assert isinstance(exc, LoadError)


def test_message_names_live_sidecar_and_reason() -> None:
    msg = str(StateDecodeError(
        live=Path("/ctl/.imp/changes/abc/state/state.json"),
        sidecar=Path("/ctl/.imp/changes/abc/state/state.json.corrupt-X-1"),
        reason="Extra data: line 2 column 1 (char 31)",
    ))
    assert "/ctl/.imp/changes/abc/state/state.json" in msg            # live file
    assert "state.json.corrupt-X-1" in msg                           # sidecar
    assert "Extra data: line 2 column 1 (char 31)" in msg            # stdlib reason
    assert not msg.startswith("imp:")                                # caller adds prefix


def test_message_notes_sidecar_unavailable_when_none() -> None:
    msg = str(StateDecodeError(
        live=Path("/ctl/.imp/changes/abc/state/state.json"),
        sidecar=None,
        reason="Extra data: line 2 column 1 (char 31)",
    ))
    assert "/ctl/.imp/changes/abc/state/state.json" in msg
    assert "Extra data: line 2 column 1 (char 31)" in msg
    # When the sidecar write failed, the message says so rather than pointing
    # at a path that does not exist.
    assert "could not" in msg.lower() or "unavailable" in msg.lower()

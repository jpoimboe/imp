from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest

from imp.engine import readfault
from imp.engine.state import InMemoryEventLog
from imp.paths import display_path
from tests._dac import dac_enforced
from tests._faultlog import diag, fault_lines

_GOOD = b'{"id": "c1", "step": null, "step_attempt": 0, "status": "new"}\n'
_HEAD_DROP = _GOOD[1:]      # the observed btrfs+zstd signature
_TAIL_DROP = _GOOD[:-1]

_STATE_PATH = Path("/q/projects/imp/quests/sifo63/state/state.json")
_REASON = "Expecting value: line 1 column 1 (char 0)"


def _record_recovered_head_drop() -> Path | None:
    return readfault.record(
        "state", _STATE_PATH, attempt=2, reason=_REASON,
        bad=_HEAD_DROP, good=_GOOD)


def test_recovered_record_line_carries_every_field() -> None:
    _record_recovered_head_drop()
    lines = fault_lines()
    assert len(lines) == 1
    line = lines[0]
    assert line["site"] == "state"
    assert line["path"] == str(_STATE_PATH)
    assert line["pid"] == os.getpid()
    assert line["recovered"] is True
    assert line["attempt"] == 2
    assert line["reason"] == _REASON
    assert line["bad_len"] == len(_GOOD) - 1
    assert line["good_len"] == len(_GOOD)
    assert line["head_drop"] is True
    assert line["tail_drop"] is False
    assert line["bad_sha1"] == hashlib.sha1(_HEAD_DROP).hexdigest()
    assert line["good_sha1"] == hashlib.sha1(_GOOD).hexdigest()
    assert isinstance(line["bytes"], str)
    assert line["bytes"].startswith("bytes/") and line["bytes"].endswith(".bad")
    # ISO-8601 UTC, e.g. 2026-08-11T18:03:11Z
    assert len(line["time"]) == 20 and line["time"][10] == "T"
    assert line["time"].endswith("Z")


def test_tail_drop_shape_is_fingerprinted_distinctly() -> None:
    readfault.record(
        "archive_record", Path("/q/archive/records/sifo63.json"),
        attempt=1, reason=_REASON, bad=_TAIL_DROP, good=_GOOD)
    line = fault_lines()[0]
    assert line["site"] == "archive_record"
    assert line["head_drop"] is False
    assert line["tail_drop"] is True
    assert line["bad_len"] == len(_GOOD) - 1


def test_unrecovered_record_line_nulls_the_good_side() -> None:
    readfault.record(
        "state", _STATE_PATH, attempt=None, reason=_REASON,
        bad=_HEAD_DROP, good=None)
    line = fault_lines()[0]
    assert line["recovered"] is False
    assert line["attempt"] is None
    assert line["good_len"] is None
    assert line["head_drop"] is None
    assert line["tail_drop"] is None
    assert line["good_sha1"] is None
    assert line["bad_len"] == len(_HEAD_DROP)
    assert line["bad_sha1"] == hashlib.sha1(_HEAD_DROP).hexdigest()
    assert line["bytes"] is not None


def test_blob_preserves_bad_bytes_and_is_reachable_from_the_line() -> None:
    blob = _record_recovered_head_drop()
    line = fault_lines()[0]
    assert line["bytes"].startswith("bytes/")
    assert line["bytes"].endswith(".bad")
    resolved = diag() / line["bytes"]
    assert resolved.read_bytes() == _HEAD_DROP
    assert blob == resolved


def test_blob_name_carries_pid_site_and_a_per_process_sequence() -> None:
    first = _record_recovered_head_drop()
    second = _record_recovered_head_drop()
    assert first is not None and second is not None
    assert first != second
    lines = fault_lines()
    assert len(lines) == 2
    assert lines[0]["bytes"] != lines[1]["bytes"]
    for blob in (first, second):
        assert blob.parent == diag() / "bytes"
        assert f"-{os.getpid()}-state-" in blob.name
        assert blob.read_bytes() == _HEAD_DROP


def test_blob_write_failure_records_null_bytes_and_returns_none() -> None:
    (diag() / "bytes").mkdir(parents=True)
    (diag() / "bytes").chmod(0o500)
    try:
        with dac_enforced():
            blob = _record_recovered_head_drop()
    finally:
        (diag() / "bytes").chmod(0o700)
    assert blob is None
    line = fault_lines()[0]
    assert line["bytes"] is None
    assert line["bad_sha1"] == hashlib.sha1(_HEAD_DROP).hexdigest()


def test_unwritable_diagnostics_dir_is_inert() -> None:
    diag().mkdir(parents=True)
    diag().chmod(0o500)
    try:
        with dac_enforced():
            blob = _record_recovered_head_drop()      # must not raise
    finally:
        diag().chmod(0o700)
    assert blob is None
    assert not (diag() / "read-faults.jsonl").exists()


def _warnings(log: InMemoryEventLog) -> list[dict[str, Any]]:
    return [e for e in log.read() if e["type"] == "runner_warning"]


def _evidence() -> str:
    return display_path(diag() / "read-faults.jsonl")


def test_recovered_state_fault_emits_exact_warning_copy() -> None:
    log = InMemoryEventLog()
    with readfault.event_sink(log):
        readfault.record(
            "state", _STATE_PATH, attempt=1, reason=_REASON,
            bad=_HEAD_DROP, good=_GOOD)
    warnings = _warnings(log)
    assert len(warnings) == 1
    assert warnings[0]["agent_id"] is None
    payload = warnings[0]["payload"]
    assert payload["message"] == (
        "transient read fault recovered — state.json re-read cleanly "
        "on attempt 1 (head-drop)")
    assert payload["detail"] == (
        f"{_STATE_PATH} — bad {len(_HEAD_DROP)}B, good {len(_GOOD)}B; "
        f"evidence: {_evidence()}")
    assert len(fault_lines()) == 1


def test_archive_record_recovery_names_the_record_file() -> None:
    log = InMemoryEventLog()
    with readfault.event_sink(log):
        readfault.record(
            "archive_record", Path("/q/archive/records/sifo63.json"),
            attempt=1, reason=_REASON, bad=_TAIL_DROP,
            good=_GOOD)
    assert _warnings(log)[0]["payload"]["message"] == (
        "transient read fault recovered — archive record sifo63.json re-read "
        "cleanly on attempt 1 (tail-drop)")


def test_unrecognised_shape_says_other_shape() -> None:
    log = InMemoryEventLog()
    with readfault.event_sink(log):
        readfault.record(
            "state", _STATE_PATH, attempt=3, reason=_REASON,
            bad=b"totally unrelated bytes", good=_GOOD)
    assert _warnings(log)[0]["payload"]["message"] == (
        "transient read fault recovered — state.json re-read cleanly "
        "on attempt 3 (other shape)")


def test_unrecovered_fault_emits_no_shape_parenthetical() -> None:
    log = InMemoryEventLog()
    with readfault.event_sink(log):
        readfault.record(
            "state", _STATE_PATH, attempt=None,
            reason=_REASON, bad=_HEAD_DROP, good=None)
    payload = _warnings(log)[0]["payload"]
    assert payload["message"] == (
        "read fault NOT recovered — state.json still unparseable "
        "after 4 re-reads")
    assert "(" not in payload["message"]
    assert payload["detail"] == (
        f"{_STATE_PATH} — bad {len(_HEAD_DROP)}B; evidence: {_evidence()}")


def test_event_tail_site_records_but_never_emits() -> None:
    log = InMemoryEventLog()
    with readfault.event_sink(log):
        readfault.record(
            "event_tail", Path("/q/quests/sifo63/events.jsonl"),
            attempt=None, reason=_REASON, bad=_HEAD_DROP,
            good=None)
    assert _warnings(log) == []
    assert fault_lines()[0]["site"] == "event_tail"


def test_sink_is_cleared_when_the_context_exits() -> None:
    log = InMemoryEventLog()
    with readfault.event_sink(log):
        pass
    _record_recovered_head_drop()
    assert _warnings(log) == []
    assert len(fault_lines()) == 1


def test_sink_is_cleared_when_the_block_raises() -> None:
    log = InMemoryEventLog()
    with pytest.raises(RuntimeError, match="boom"), readfault.event_sink(log):
        raise RuntimeError("boom")
    _record_recovered_head_drop()          # a crashed run must not leave a dead log installed
    assert _warnings(log) == []
    assert len(fault_lines()) == 1


def test_a_raising_sink_does_not_break_the_file_record() -> None:
    class _Boom:
        def append(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("sink down")

    with readfault.event_sink(_Boom()):        # type: ignore[arg-type]
        blob = _record_recovered_head_drop()
    assert blob is not None
    assert len(fault_lines()) == 1
    assert fault_lines()[0]["head_drop"] is True

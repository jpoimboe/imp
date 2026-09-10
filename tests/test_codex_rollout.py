"""Unit tests for codex rollout persistence, restore, and live readings."""
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from imp.engine.resume_state import (
    InvalidResumeState,
    NativeResumeState,
    read_native_resume_state,
)
from imp.providers import codex_rollout
from imp.providers.codex_rollout import (
    CorrelatedReadings,
    RolloutCursor,
    RolloutTelemetry,
    RolloutUsage,
    compaction_seen,
    persist_rollout,
    quota_from_rate_limits,
    restore_rollout,
)

_THREAD_ID = "01991f4d-3dc2-7c90-a41f-3cbbd120a941"
_RESTORE_PATH = (
    "sessions/2026/09/10/rollout-2026-09-10T00-00-00-"
    f"{_THREAD_ID}.jsonl"
)
_SLOT_A = "codex-rollout-a.jsonl"
_SLOT_B = "codex-rollout-b.jsonl"


def _plant_rollout(codex_home: Path, thread_id: str,
                   body: str = '{"codeword": "MANGO-5512"}\n') -> Path:
    rel = Path("sessions/2026/06/05") / f"rollout-2026-06-05T00-00-00-{thread_id}.jsonl"
    dest = codex_home / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body)
    return dest


def _resume_state(
    *,
    provider: object = "codex",
    state: dict[str, object] | None = None,
) -> NativeResumeState:
    return NativeResumeState(
        provider=provider,  # type: ignore[arg-type]
        state=state or {
            "thread_id": _THREAD_ID,
            "restore_path": _RESTORE_PATH,
            "rollout_slot": _SLOT_A,
        },
    )


def _seed_slot(
    transcript_dir: Path,
    *,
    slot: str = _SLOT_A,
    body: bytes = b'{"id":"old"}\n',
) -> NativeResumeState:
    transcript_dir.mkdir(parents=True, exist_ok=True)
    state = _resume_state(state={
        "thread_id": _THREAD_ID,
        "restore_path": _RESTORE_PATH,
        "rollout_slot": slot,
    })
    (transcript_dir / slot).write_bytes(body)
    return state


def _published_state(transcript_dir: Path) -> NativeResumeState:
    state = read_native_resume_state(transcript_dir)
    assert state is not None
    return state


def test_persist_publishes_exact_manifest_and_first_slot(tmp_path: Path) -> None:
    home = tmp_path / "codex_home"
    rollout = _plant_rollout(home, _THREAD_ID)
    tx = tmp_path / "tx"

    assert persist_rollout(
        codex_home=home, thread_id=_THREAD_ID, transcript_dir=tx) is True

    assert _published_state(tx) == NativeResumeState(provider="codex", state={
        "thread_id": _THREAD_ID,
        "restore_path": rollout.relative_to(home).as_posix(),
        "rollout_slot": _SLOT_A,
    })
    assert (tx / _SLOT_A).read_bytes() == rollout.read_bytes()
    assert not (tx / "codex_rollout").exists()
    assert not (tx / "codex_thread.json").exists()


def test_persist_ignores_sole_rollout_for_another_thread(tmp_path: Path) -> None:
    home = tmp_path / "codex_home"
    _plant_rollout(home, "another-thread")
    tx = tmp_path / "tx"
    tx.mkdir()

    assert persist_rollout(
        codex_home=home, thread_id=_THREAD_ID, transcript_dir=tx) is False
    assert list(tx.iterdir()) == []


def test_republish_alternates_slots_and_bounds_storage(tmp_path: Path) -> None:
    home = tmp_path / "codex_home"
    rollout = _plant_rollout(home, _THREAD_ID, "OLD")
    tx = tmp_path / "tx"

    assert persist_rollout(
        codex_home=home, thread_id=_THREAD_ID, transcript_dir=tx) is True
    rollout.write_text("NEW")
    assert persist_rollout(
        codex_home=home, thread_id=_THREAD_ID, transcript_dir=tx) is True

    assert (tx / _SLOT_A).read_bytes() == b"OLD"
    assert (tx / _SLOT_B).read_bytes() == b"NEW"
    assert _published_state(tx).state["rollout_slot"] == _SLOT_B
    assert {path.name for path in tx.iterdir()} == {
        _SLOT_A, _SLOT_B, "resume-state.json"}


def test_restore_copies_selected_bytes_to_exact_path(tmp_path: Path) -> None:
    tx = tmp_path / "tx"
    state = _seed_slot(tx, slot=_SLOT_B, body=b'{"selected":true}\n')
    home = tmp_path / "codex_home"

    thread_id = restore_rollout(
        codex_home=home, transcript_dir=tx, resume_state=state)

    assert thread_id == _THREAD_ID
    assert (home / _RESTORE_PATH).read_bytes() == b'{"selected":true}\n'


def test_restore_rejects_destination_resolving_outside_home(
    tmp_path: Path,
) -> None:
    tx = tmp_path / "tx"
    state = _seed_slot(tx)
    home = tmp_path / "codex_home"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    (home / "sessions").symlink_to(outside, target_is_directory=True)

    with pytest.raises(InvalidResumeState):
        restore_rollout(codex_home=home, transcript_dir=tx, resume_state=state)

    assert list(outside.iterdir()) == []


_INVALID_STATES: list[tuple[str, NativeResumeState]] = [
    ("missing key", _resume_state(state={
        "thread_id": _THREAD_ID, "restore_path": _RESTORE_PATH})),
    ("extra key", _resume_state(state={
        "thread_id": _THREAD_ID, "restore_path": _RESTORE_PATH,
        "rollout_slot": _SLOT_A, "extra": True})),
    ("empty thread", _resume_state(state={
        "thread_id": "", "restore_path": _RESTORE_PATH, "rollout_slot": _SLOT_A})),
    ("non-string thread", _resume_state(state={
        "thread_id": 1, "restore_path": _RESTORE_PATH, "rollout_slot": _SLOT_A})),
    ("empty path", _resume_state(state={
        "thread_id": _THREAD_ID, "restore_path": "", "rollout_slot": _SLOT_A})),
    ("non-string path", _resume_state(state={
        "thread_id": _THREAD_ID, "restore_path": 1, "rollout_slot": _SLOT_A})),
    ("absolute path", _resume_state(state={
        "thread_id": _THREAD_ID, "restore_path": "/sessions/x.jsonl",
        "rollout_slot": _SLOT_A})),
    ("double slash", _resume_state(state={
        "thread_id": _THREAD_ID, "restore_path": "sessions//x.jsonl",
        "rollout_slot": _SLOT_A})),
    ("dot component", _resume_state(state={
        "thread_id": _THREAD_ID, "restore_path": "sessions/./x.jsonl",
        "rollout_slot": _SLOT_A})),
    ("dotdot component", _resume_state(state={
        "thread_id": _THREAD_ID, "restore_path": "sessions/../x.jsonl",
        "rollout_slot": _SLOT_A})),
    ("backslash", _resume_state(state={
        "thread_id": _THREAD_ID, "restore_path": "sessions\\x.jsonl",
        "rollout_slot": _SLOT_A})),
    ("nul", _resume_state(state={
        "thread_id": _THREAD_ID, "restore_path": "sessions/x\0.jsonl",
        "rollout_slot": _SLOT_A})),
    ("wrong root", _resume_state(state={
        "thread_id": _THREAD_ID, "restore_path": "other/x.jsonl",
        "rollout_slot": _SLOT_A})),
    ("wrong suffix", _resume_state(state={
        "thread_id": _THREAD_ID, "restore_path": "sessions/x.json",
        "rollout_slot": _SLOT_A})),
    ("invalid slot", _resume_state(state={
        "thread_id": _THREAD_ID, "restore_path": _RESTORE_PATH,
        "rollout_slot": "rollout.jsonl"})),
    ("non-string slot", _resume_state(state={
        "thread_id": _THREAD_ID, "restore_path": _RESTORE_PATH, "rollout_slot": 1})),
    ("wrong provider", _resume_state(provider="claude_code")),
]


@pytest.mark.parametrize(("case", "state"), _INVALID_STATES, ids=lambda value: str(value))
def test_restore_rejects_invalid_state_before_home_mutation(
    tmp_path: Path,
    case: str,
    state: NativeResumeState,
) -> None:
    tx = tmp_path / "tx"
    _seed_slot(tx)
    home = tmp_path / "codex_home"

    with pytest.raises(InvalidResumeState):
        restore_rollout(codex_home=home, transcript_dir=tx, resume_state=state)

    assert not home.exists(), case


@pytest.mark.parametrize(
    ("selected_kind", "error"),
    [("missing", FileNotFoundError), ("directory", InvalidResumeState),
     ("symlink", InvalidResumeState)],
)
def test_restore_rejects_non_regular_selected_slot_before_home_mutation(
    tmp_path: Path,
    selected_kind: str,
    error: type[Exception],
) -> None:
    tx = tmp_path / "tx"
    tx.mkdir()
    selected = tx / _SLOT_A
    if selected_kind == "directory":
        selected.mkdir()
    elif selected_kind == "symlink":
        target = tx / "target.jsonl"
        target.write_bytes(b"target")
        selected.symlink_to(target)
    home = tmp_path / "codex_home"

    with pytest.raises(error):
        restore_rollout(
            codex_home=home, transcript_dir=tx, resume_state=_resume_state())

    assert not home.exists()


@pytest.mark.parametrize(
    "failure_point",
    ["slot_replace", "slot_dir_fsync", "manifest_file_fsync", "manifest_replace"],
)
def test_republish_failure_preserves_published_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    home = tmp_path / "codex_home"
    rollout = _plant_rollout(home, _THREAD_ID, "OLD")
    tx = tmp_path / "tx"
    persist_rollout(codex_home=home, thread_id=_THREAD_ID, transcript_dir=tx)
    old_manifest = (tx / "resume-state.json").read_bytes()
    rollout.write_text("NEW")

    real_fsync_file = codex_rollout._fsync_file
    real_fsync_directory = codex_rollout._fsync_directory
    real_replace = os.replace
    file_fsync_count = 0
    directory_fsync_count = 0

    def fail_file_fsync(stream: Any, path: Path) -> None:
        nonlocal file_fsync_count
        file_fsync_count += 1
        if failure_point == "manifest_file_fsync" and file_fsync_count == 2:
            raise OSError("injected")
        real_fsync_file(stream, path)

    def fail_directory_fsync(path: Path) -> None:
        nonlocal directory_fsync_count
        directory_fsync_count += 1
        if failure_point == "slot_dir_fsync" and directory_fsync_count == 1:
            raise OSError("injected")
        real_fsync_directory(path)

    def fail_replace(src: Path, dest: Path) -> None:
        if failure_point == "slot_replace" and Path(dest).name == _SLOT_B:
            raise OSError("injected")
        if failure_point == "manifest_replace" and Path(dest).name == "resume-state.json":
            raise OSError("injected")
        real_replace(src, dest)

    monkeypatch.setattr(codex_rollout, "_fsync_file", fail_file_fsync)
    monkeypatch.setattr(codex_rollout, "_fsync_directory", fail_directory_fsync)
    monkeypatch.setattr(codex_rollout.os, "replace", fail_replace)

    with pytest.raises(OSError, match="injected"):
        persist_rollout(codex_home=home, thread_id=_THREAD_ID, transcript_dir=tx)

    assert (tx / "resume-state.json").read_bytes() == old_manifest
    assert (tx / _SLOT_A).read_bytes() == b"OLD"


@pytest.mark.parametrize(
    "failure_point", ["slot_file_fsync", "slot_replace", "slot_dir_fsync"])
def test_first_publish_failure_never_publishes_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    home = tmp_path / "codex_home"
    _plant_rollout(home, _THREAD_ID, "NEW")
    tx = tmp_path / "tx"

    real_fsync_file = codex_rollout._fsync_file
    real_fsync_directory = codex_rollout._fsync_directory
    real_replace = os.replace

    def fail_file_fsync(stream: Any, path: Path) -> None:
        if failure_point == "slot_file_fsync":
            raise OSError("injected")
        real_fsync_file(stream, path)

    def fail_directory_fsync(path: Path) -> None:
        if failure_point == "slot_dir_fsync":
            raise OSError("injected")
        real_fsync_directory(path)

    def fail_replace(src: Path, dest: Path) -> None:
        if failure_point == "slot_replace":
            raise OSError("injected")
        real_replace(src, dest)

    monkeypatch.setattr(codex_rollout, "_fsync_file", fail_file_fsync)
    monkeypatch.setattr(codex_rollout, "_fsync_directory", fail_directory_fsync)
    monkeypatch.setattr(codex_rollout.os, "replace", fail_replace)

    with pytest.raises(OSError, match="injected"):
        persist_rollout(codex_home=home, thread_id=_THREAD_ID, transcript_dir=tx)

    assert not (tx / "resume-state.json").exists()


def test_publish_durability_call_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "codex_home"
    _plant_rollout(home, _THREAD_ID)
    tx = tmp_path / "tx"
    calls: list[str] = []
    real_fsync_file = codex_rollout._fsync_file
    real_fsync_directory = codex_rollout._fsync_directory
    real_replace = os.replace

    def record_file_fsync(stream: Any, path: Path) -> None:
        calls.append("slot file-fsync" if not calls else "manifest file-fsync")
        real_fsync_file(stream, path)

    def record_replace(src: Path, dest: Path) -> None:
        calls.append(
            "manifest replace" if Path(dest).name == "resume-state.json"
            else "slot replace")
        real_replace(src, dest)

    def record_directory_fsync(path: Path) -> None:
        calls.append("dir-fsync")
        real_fsync_directory(path)

    monkeypatch.setattr(codex_rollout, "_fsync_file", record_file_fsync)
    monkeypatch.setattr(codex_rollout.os, "replace", record_replace)
    monkeypatch.setattr(codex_rollout, "_fsync_directory", record_directory_fsync)

    persist_rollout(codex_home=home, thread_id=_THREAD_ID, transcript_dir=tx)

    assert calls == [
        "slot file-fsync", "slot replace", "dir-fsync",
        "manifest file-fsync", "manifest replace", "dir-fsync",
    ]


def test_manifest_directory_fsync_failure_leaves_selected_pair_usable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "codex_home"
    rollout = _plant_rollout(home, _THREAD_ID, "OLD")
    tx = tmp_path / "tx"
    persist_rollout(codex_home=home, thread_id=_THREAD_ID, transcript_dir=tx)
    rollout.write_text("NEW")
    real_fsync_directory = codex_rollout._fsync_directory
    calls = 0

    def fail_second_directory_fsync(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected")
        real_fsync_directory(path)

    monkeypatch.setattr(
        codex_rollout, "_fsync_directory", fail_second_directory_fsync)

    with pytest.raises(OSError, match="injected"):
        persist_rollout(codex_home=home, thread_id=_THREAD_ID, transcript_dir=tx)

    state = _published_state(tx)
    assert state.state["rollout_slot"] == _SLOT_B
    assert (tx / _SLOT_A).read_bytes() == b"OLD"
    assert (tx / _SLOT_B).read_bytes() == b"NEW"


_WINDOW = 258400
_RATE_LIMITS = {
    "limit_id": "codex", "limit_name": None,
    "primary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": 1788403732},
    "secondary": {"used_percent": 14.0, "window_minutes": 10080, "resets_at": 1788971433},
    "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
    "individual_limit": None, "spend_control_reached": None,
    "plan_type": "plus", "rate_limit_reached_type": None,
}


_USAGE = RolloutUsage(
    input_tokens=82628,
    cached_input_tokens=80000,
    cache_write_input_tokens=7,
    output_tokens=258,
    reasoning_output_tokens=63,
)


def _usage(**changes: object) -> RolloutUsage:
    values = {
        "input_tokens": _USAGE.input_tokens,
        "cached_input_tokens": _USAGE.cached_input_tokens,
        "cache_write_input_tokens": _USAGE.cache_write_input_tokens,
        "output_tokens": _USAGE.output_tokens,
        "reasoning_output_tokens": _USAGE.reasoning_output_tokens,
    }
    values.update(changes)
    return RolloutUsage(**values)  # type: ignore[arg-type]


def _token_count_line(*, usage: RolloutUsage = _USAGE,
                      total_tokens: object = 22389, window: object = _WINDOW,
                      rate_limits: object = None) -> str:
    return json.dumps({
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": usage.input_tokens,
                    "cached_input_tokens": usage.cached_input_tokens,
                    "cache_write_input_tokens": usage.cache_write_input_tokens,
                    "output_tokens": usage.output_tokens,
                    "reasoning_output_tokens": usage.reasoning_output_tokens,
                },
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
            "rate_limits": rate_limits,
        },
    }) + "\n"


def _home_with(tmp_path: Path, body: str) -> Path:
    home = tmp_path / "codex_home"
    home.mkdir()
    _plant_rollout(home, "thr-1", body)
    return home


def test_rollout_telemetry_correlates_only_final_exact_usage(tmp_path: Path) -> None:
    home = _home_with(tmp_path, _token_count_line(usage=_usage(output_tokens=1)))
    telemetry = RolloutTelemetry(home, "thr-1")
    cursor = telemetry.begin_turn()
    rollout = next((home / "sessions").rglob("*.jsonl"))
    with rollout.open("a") as stream:
        stream.write(_token_count_line(usage=_usage(output_tokens=2), total_tokens=21000))
        stream.write(_token_count_line(rate_limits=_RATE_LIMITS))

    result = telemetry.scan_current(cursor, _USAGE)

    assert (
        result.matched,
        result.readings.context,
        result.readings.quota.plan if result.readings.quota else None,
    ) == (True, (22389, _WINDOW), "plus")


@pytest.mark.parametrize("field", [
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
])
def test_rollout_telemetry_rejects_each_nonexact_aggregate_field(
        tmp_path: Path, field: str) -> None:
    home = _home_with(tmp_path, "")
    telemetry = RolloutTelemetry(home, "thr-1")
    cursor = telemetry.begin_turn()
    rollout = next((home / "sessions").rglob("*.jsonl"))
    with rollout.open("a") as stream:
        different = _usage(**{field: getattr(_USAGE, field) + 1})
        stream.write(_token_count_line(usage=different))

    result: CorrelatedReadings = telemetry.scan_current(cursor, _USAGE)

    assert (
        result.matched, result.readings.context, result.readings.quota,
    ) == (False, None, None)


@pytest.mark.parametrize("field", [
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
])
def test_rollout_telemetry_rejects_bool_in_each_aggregate_field(
        tmp_path: Path, field: str) -> None:
    home = _home_with(tmp_path, "")
    telemetry = RolloutTelemetry(home, "thr-1")
    cursor = telemetry.begin_turn()
    rollout = next((home / "sessions").rglob("*.jsonl"))
    with rollout.open("a") as stream:
        stream.write(_token_count_line(usage=_usage(**{field: True})))

    result = telemetry.scan_current(cursor, _USAGE)

    assert result.matched is False


@pytest.mark.parametrize(("total_tokens", "window"), [
    (True, _WINDOW),
    (22389, True),
])
def test_rollout_telemetry_bool_context_is_matched_but_unusable(
        tmp_path: Path, total_tokens: object, window: object) -> None:
    home = _home_with(tmp_path, "")
    telemetry = RolloutTelemetry(home, "thr-1")
    cursor = telemetry.begin_turn()
    rollout = next((home / "sessions").rglob("*.jsonl"))
    with rollout.open("a") as stream:
        stream.write(_token_count_line(total_tokens=total_tokens, window=window,
                                       rate_limits=_RATE_LIMITS))

    result = telemetry.scan_current(cursor, _USAGE)

    assert (result.matched, result.readings.context, result.readings.quota) == (
        True, None, None)


@pytest.mark.parametrize("window", [
    {"window_minutes": True, "used_percent": 2.0},
    {"window_minutes": 300, "used_percent": False},
])
def test_quota_from_rate_limits_rejects_bool_required_values(window: object) -> None:
    assert quota_from_rate_limits({"primary": window}) is None


def test_quota_from_rate_limits_drops_bool_reset() -> None:
    reading = quota_from_rate_limits({
        "primary": {"window_minutes": 300, "used_percent": 2.0, "resets_at": True},
    })

    assert reading is not None and reading.windows[0].resets_at is None


def test_rollout_telemetry_retries_partial_line_after_newline(tmp_path: Path) -> None:
    home = _home_with(tmp_path, "")
    telemetry = RolloutTelemetry(home, "thr-1")
    cursor = telemetry.begin_turn()
    rollout = next((home / "sessions").rglob("*.jsonl"))
    with rollout.open("a") as stream:
        stream.write(_token_count_line().removesuffix("\n"))
    first = telemetry.scan_current(cursor, _USAGE)
    with rollout.open("a") as stream:
        stream.write("\n")
    second = telemetry.scan_current(cursor, _USAGE)

    assert (first.matched, second.matched, second.readings.context) == (
        False, True, (22389, _WINDOW))


def test_rollout_telemetry_consumes_newline_complete_malformed_record(
        tmp_path: Path) -> None:
    malformed = b'{"type":"event_msg","payload":{"type":"token_count"}\n'
    home = _home_with(tmp_path, malformed.decode())
    telemetry = RolloutTelemetry(home, "thr-1")

    cursor: RolloutCursor = telemetry.begin_turn()

    assert cursor.entries[0][2] == len(malformed)


def test_interrupted_discovery_does_not_admit_omitted_identity_or_pending_claim(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "codex_home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    first = sessions / "rollout-a.jsonl"
    second = sessions / "rollout-b.jsonl"
    first.write_text(_token_count_line(usage=_usage(output_tokens=1)))
    second.write_text(_token_count_line())
    original_rglob = Path.rglob

    def interrupted_rglob(path: Path, pattern: str) -> Iterator[Path]:
        if path == sessions:
            yield first
            raise OSError("interrupted traversal")
        yield from original_rglob(path, pattern)

    monkeypatch.setattr(Path, "rglob", interrupted_rglob)
    telemetry = RolloutTelemetry(home, None)
    interrupted_cursor = telemetry.begin_turn()
    monkeypatch.setattr(Path, "rglob", original_rglob)
    missed = telemetry.scan_current(interrupted_cursor, _USAGE)
    telemetry.finish_turn(interrupted_cursor, _USAGE, missed.matched)
    recovered_cursor = telemetry.begin_turn()
    with second.open("a") as stream:
        stream.write(_token_count_line(total_tokens=24000))

    recovered = telemetry.scan_current(recovered_cursor, _USAGE)

    assert (missed.matched, recovered.matched, recovered.readings.context) == (
        False, True, (24000, _WINDOW))


def test_interrupted_discovery_excludes_previously_known_omitted_identity(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "codex_home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    first = sessions / "rollout-a.jsonl"
    second = sessions / "rollout-b.jsonl"
    first.write_text("")
    second.write_text("")
    telemetry = RolloutTelemetry(home, None)
    initial_cursor = telemetry.begin_turn()
    pending_usage = _usage(output_tokens=700)
    telemetry.finish_turn(initial_cursor, pending_usage, matched=False)
    with second.open("a") as stream:
        stream.write(_token_count_line(usage=pending_usage, total_tokens=23000))
    original_rglob = Path.rglob

    def interrupted_rglob(path: Path, pattern: str) -> Iterator[Path]:
        if path == sessions:
            yield first
            raise OSError("interrupted traversal")
        yield from original_rglob(path, pattern)

    monkeypatch.setattr(Path, "rglob", interrupted_rglob)
    interrupted_cursor = telemetry.begin_turn()
    monkeypatch.setattr(Path, "rglob", original_rglob)
    current_usage = _usage(output_tokens=800)
    with second.open("a") as stream:
        stream.write(_token_count_line(usage=current_usage, total_tokens=24000))
    missed = telemetry.scan_current(interrupted_cursor, current_usage)
    pending_during_interruption = tuple(telemetry._pending)
    telemetry.finish_turn(interrupted_cursor, current_usage, missed.matched)
    recovered_cursor = telemetry.begin_turn()
    pending_after_recovery = tuple(telemetry._pending)
    with second.open("a") as stream:
        stream.write(_token_count_line(usage=current_usage, total_tokens=25000))

    recovered = telemetry.scan_current(recovered_cursor, current_usage)

    assert (
        missed.matched,
        pending_during_interruption,
        pending_after_recovery,
        recovered.matched,
        recovered.readings.context,
    ) == (False, (pending_usage,), (), True, (25000, _WINDOW))


def test_new_generations_are_ordered_by_mtime_then_path_not_traversal(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "codex_home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    telemetry = RolloutTelemetry(home, None)
    cursor = telemetry.begin_turn()
    first = sessions / "rollout-a.jsonl"
    second = sessions / "rollout-b.jsonl"
    first.write_text(_token_count_line(total_tokens=21000))
    second.write_text(_token_count_line(total_tokens=22000))
    timestamp = 1_800_000_000_000_000_000
    os.utime(first, ns=(timestamp, timestamp))
    os.utime(second, ns=(timestamp, timestamp))
    original_rglob = Path.rglob

    def reverse_rglob(path: Path, pattern: str) -> Iterator[Path]:
        if path == sessions:
            yield second
            yield first
            return
        yield from original_rglob(path, pattern)

    monkeypatch.setattr(Path, "rglob", reverse_rglob)

    result = telemetry.scan_current(cursor, _USAGE)

    assert result.readings.context == (21000, _WINDOW)


def test_stale_generation_rejects_same_path_inode_replacement(tmp_path: Path) -> None:
    home = _home_with(tmp_path, "")
    telemetry = RolloutTelemetry(home, "thr-1")
    telemetry.begin_turn()
    generation = telemetry._generations[0]
    rollout = generation.path
    rollout.unlink()
    rollout.write_text(_token_count_line())

    assert list(telemetry._records(generation)) == []


def test_late_record_before_next_cursor_clears_pending_turn(tmp_path: Path) -> None:
    home = _home_with(tmp_path, "")
    telemetry = RolloutTelemetry(home, "thr-1")
    first_cursor = telemetry.begin_turn()
    telemetry.finish_turn(first_cursor, _USAGE, matched=False)
    rollout = next((home / "sessions").rglob("*.jsonl"))
    with rollout.open("a") as stream:
        stream.write(_token_count_line())
    second_cursor = telemetry.begin_turn()

    assert telemetry._pending == []
    assert [frontier for _, _, frontier in second_cursor.entries] == [
        rollout.stat().st_size,
    ]

    current_usage = _USAGE
    with rollout.open("a") as stream:
        stream.write(_token_count_line(usage=current_usage, total_tokens=25000))

    result = telemetry.scan_current(second_cursor, current_usage)

    assert (result.matched, result.readings.context) == (True, (25000, _WINDOW))


def test_scan_current_drains_complete_records_after_first_usable_match(
        tmp_path: Path) -> None:
    home = _home_with(tmp_path, "")
    telemetry = RolloutTelemetry(home, "thr-1")
    first_cursor = telemetry.begin_turn()
    pending_usage = _usage(output_tokens=700)
    telemetry.finish_turn(first_cursor, pending_usage, matched=False)
    cursor = telemetry.begin_turn()
    rollout = next((home / "sessions").rglob("*.jsonl"))
    current_usage = _usage(output_tokens=800)
    with rollout.open("a") as stream:
        stream.write(_token_count_line(usage=current_usage, total_tokens=25000))
        stream.write('{"irrelevant": true}\n')
    later_rollout = home / "sessions" / "rollout-later-thr-1.jsonl"
    later_rollout.write_text(
        _token_count_line(usage=pending_usage, total_tokens=26000))

    result = telemetry.scan_current(cursor, current_usage)

    assert (
        result.matched,
        result.readings.context,
        [generation.frontier for generation in telemetry._generations],
        telemetry._pending,
    ) == (
        True,
        (25000, _WINDOW),
        [rollout.stat().st_size, later_rollout.stat().st_size],
        [],
    )


def test_late_record_after_next_cursor_requires_second_identical_match(
        tmp_path: Path) -> None:
    home = _home_with(tmp_path, "")
    telemetry = RolloutTelemetry(home, "thr-1")
    first_cursor = telemetry.begin_turn()
    telemetry.finish_turn(first_cursor, _USAGE, matched=False)
    second_cursor = telemetry.begin_turn()
    rollout = next((home / "sessions").rglob("*.jsonl"))
    with rollout.open("a") as stream:
        stream.write(_token_count_line(total_tokens=23000))
    first = telemetry.scan_current(second_cursor, _USAGE)
    with rollout.open("a") as stream:
        stream.write(_token_count_line(total_tokens=24000))
    second = telemetry.scan_current(second_cursor, _USAGE)

    assert (first.matched, second.matched, second.readings.context) == (
        False, True, (24000, _WINDOW))


def test_unusable_correlated_record_leaves_no_late_claim(tmp_path: Path) -> None:
    home = _home_with(tmp_path, "")
    telemetry = RolloutTelemetry(home, "thr-1")
    first_cursor = telemetry.begin_turn()
    rollout = next((home / "sessions").rglob("*.jsonl"))
    with rollout.open("a") as stream:
        stream.write(_token_count_line(window=True))
    unusable = telemetry.scan_current(first_cursor, _USAGE)
    telemetry.finish_turn(first_cursor, _USAGE, unusable.matched)
    second_cursor = telemetry.begin_turn()
    with rollout.open("a") as stream:
        stream.write(_token_count_line(total_tokens=26000))

    result = telemetry.scan_current(second_cursor, _USAGE)

    assert (unusable.matched, result.matched, result.readings.context) == (
        True, True, (26000, _WINDOW))


def _compacted_line() -> str:
    """codex's real top-level compaction record, shape-verbatim from the spike
    rollouts (`.imp/quests/sila75/artifacts/spike/rollouts/zero.rollout.jsonl`)."""
    return json.dumps({
        "timestamp": "2026-09-04T06:58:53.348Z",
        "ordinal": 18,
        "type": "compacted",
        "payload": {
            "message": "",
            "replacement_history": [
                {"type": "compaction", "id": "cmp_0d8ba4f379",
                 "encrypted_content": "gAAAAABqmmxw"},
            ],
        },
    }) + "\n"


def _decoy_lines() -> str:
    """Lines that must not trip the predicate: a truncated record the pre-filter
    hands to json.loads anyway, and an agent message whose prose says
    "compacted" without being codex's compaction record."""
    return (
        '{"type": "compacted", "payload": {"message": "trunca\n'
        + json.dumps({
            "type": "event_msg",
            "payload": {"type": "agent_message",
                        "message": "the history was compacted upstream"},
        }) + "\n"
    )


def test_compaction_seen_true_for_a_compacted_rollout(tmp_path: Path) -> None:
    body = _decoy_lines() + _token_count_line() + _compacted_line()
    home = _home_with(tmp_path, body)
    assert compaction_seen(home, "thr-1") is True


def test_compaction_seen_false_without_a_compacted_record(tmp_path: Path) -> None:
    # Decoys carry the substring; a bare match with no type check would return True.
    body = _decoy_lines() + _token_count_line()
    home = _home_with(tmp_path, body)
    assert compaction_seen(home, "thr-1") is False


def test_compaction_seen_false_when_rollout_absent(tmp_path: Path) -> None:
    home = tmp_path / "codex_home"
    (home / "sessions").mkdir(parents=True)  # exists but empty
    assert compaction_seen(home, "thr-1") is False


def test_quota_from_rate_limits_labels_each_window_from_its_own_duration() -> None:
    reading = quota_from_rate_limits(_RATE_LIMITS)
    assert reading is not None
    assert reading.provider == "codex"
    assert reading.plan == "plus"
    assert [(w.label, w.used_pct, w.resets_at) for w in reading.windows] == [
        ("5h", 12.0, 1788403732), ("7d", 14.0, 1788971433)]


def test_quota_from_rate_limits_handles_the_pro_shape() -> None:
    # On a `pro` account the WEEKLY window is reported in `primary`, with a null
    # `secondary`: nothing may assume `primary` means 5h.
    block = {"primary": {"used_percent": 31.0, "window_minutes": 10080,
                         "resets_at": 1788971433},
             "secondary": None, "plan_type": "pro"}
    reading = quota_from_rate_limits(block)
    assert reading is not None
    assert reading.plan == "pro"
    assert [(w.label, w.used_pct) for w in reading.windows] == [("7d", 31.0)]


def test_quota_from_rate_limits_orders_by_duration_not_position() -> None:
    block = {"primary": {"used_percent": 5, "window_minutes": 10080, "resets_at": 2},
             "secondary": {"used_percent": 9, "window_minutes": 300, "resets_at": 1},
             "plan_type": "plus"}
    reading = quota_from_rate_limits(block)
    assert reading is not None
    assert [(w.label, w.used_pct) for w in reading.windows] == [("5h", 9.0), ("7d", 5.0)]
    assert isinstance(reading.windows[0].used_pct, float)


def test_quota_from_rate_limits_labels_a_day_long_window_in_days() -> None:
    block = {"primary": {"used_percent": 4.0, "window_minutes": 1440, "resets_at": None}}
    reading = quota_from_rate_limits(block)
    assert reading is not None
    assert [w.label for w in reading.windows] == ["1d"]
    assert reading.windows[0].resets_at is None
    assert reading.plan is None


def test_quota_from_rate_limits_carries_the_block_verbatim() -> None:
    reading = quota_from_rate_limits(_RATE_LIMITS)
    assert reading is not None
    assert reading.raw == _RATE_LIMITS
    assert reading.raw["credits"] == {"has_credits": False, "unlimited": False,
                                      "balance": "0"}
    assert reading.raw["rate_limit_reached_type"] is None


@pytest.mark.parametrize("block", [
    None,
    {},
    {"primary": None, "secondary": None, "plan_type": "plus"},
    {"primary": {"used_percent": 3.0}},                 # no window_minutes
    {"primary": {"window_minutes": 300}},               # no used_percent
    {"primary": {"used_percent": "high", "window_minutes": 300}},
    {"primary": {"used_percent": 3.0, "window_minutes": 0}},  # zero rejected
])
def test_quota_from_rate_limits_without_a_usable_window_is_none(block: object) -> None:
    assert quota_from_rate_limits(block) is None

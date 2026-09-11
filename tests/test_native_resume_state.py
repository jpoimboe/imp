from __future__ import annotations

import pytest

from imp.engine.resume_state import (
    RESUME_STATE_NAME,
    InvalidResumeState,
    NativeResumeState,
    encode_native_resume_state,
    parse_native_resume_state,
    read_native_resume_state,
)
from imp.providers.resolve import provider_is_available, provider_is_registered


def test_missing_native_resume_state_returns_none(tmp_path) -> None:
    assert read_native_resume_state(tmp_path) is None


def test_parse_native_resume_state_accepts_valid_raw_bytes() -> None:
    raw = b'{"schema_version":1,"provider":"codex","state":{"thread_id":"thread-archive"}}'

    assert parse_native_resume_state(raw) == NativeResumeState(
        provider="codex", state={"thread_id": "thread-archive"})


def test_duplicate_schema_version_is_rejected_by_parser_and_reader(tmp_path) -> None:
    raw = b'{"schema_version":1,"schema_version":1,"provider":"codex","state":{}}'
    (tmp_path / RESUME_STATE_NAME).write_bytes(raw)

    with pytest.raises(InvalidResumeState):
        parse_native_resume_state(raw)
    with pytest.raises(InvalidResumeState):
        read_native_resume_state(tmp_path)


def test_native_resume_state_round_trips_opaque_nested_state(tmp_path) -> None:
    expected = NativeResumeState(
        provider="codex",
        state={"thread": {"id": "thread-1", "items": [{"kind": "tool", "id": 3}]}},
    )
    (tmp_path / RESUME_STATE_NAME).write_bytes(encode_native_resume_state(expected))

    assert read_native_resume_state(tmp_path) == expected


def test_native_resume_state_encoder_uses_compact_utf8_envelope() -> None:
    state = NativeResumeState(provider="codex", state={"thread_id": "café"})

    assert encode_native_resume_state(state) == (
        b'{"schema_version":1,"provider":"codex","state":{"thread_id":"caf\xc3\xa9"}}'
    )


def test_registered_unavailable_provider_is_accepted_for_native_resume_state(
    tmp_path, provider_registry,
) -> None:
    provider_registry("offline", lambda: False, lambda _model: None)
    expected = NativeResumeState(provider="offline", state={"thread_id": "thread-1"})
    (tmp_path / RESUME_STATE_NAME).write_bytes(encode_native_resume_state(expected))

    assert provider_is_registered("offline") is True
    assert provider_is_available("offline") is False
    assert read_native_resume_state(tmp_path) == expected


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff",
        b"{",
        b"[]",
        b'{"schema_version":1,"provider":"codex"}',
        b'{"schema_version":1,"provider":"codex","state":{},"extra":true}',
        b'{"schema_version":true,"provider":"codex","state":{}}',
        b'{"schema_version":1.0,"provider":"codex","state":{}}',
        b'{"schema_version":2,"provider":"codex","state":{}}',
        b'{"schema_version":1,"provider":"","state":{}}',
        b'{"schema_version":1,"provider":1,"state":{}}',
        b'{"schema_version":1,"provider":"unregistered","state":{}}',
        b'{"schema_version":1,"provider":"codex","state":[]}',
        b'{"schema_version":1,"schema_version":1,"provider":"codex","state":{}}',
        b'{"schema_version":1,"provider":"codex","state":{"nested":{"id":1,"id":2}}}',
        b'{"schema_version":NaN,"provider":"codex","state":{}}',
        b'{"schema_version":Infinity,"provider":"codex","state":{}}',
        b'{"schema_version":-Infinity,"provider":"codex","state":{}}',
    ],
)
def test_invalid_native_resume_state_envelopes_are_rejected(tmp_path, raw: bytes) -> None:
    (tmp_path / RESUME_STATE_NAME).write_bytes(raw)

    with pytest.raises(InvalidResumeState):
        read_native_resume_state(tmp_path)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_native_resume_state_encoder_rejects_non_finite_numbers(value: float) -> None:
    state = NativeResumeState(provider="codex", state={"nested": [{"value": value}]})

    with pytest.raises(InvalidResumeState):
        encode_native_resume_state(state)

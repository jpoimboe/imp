from __future__ import annotations

import json

import pytest

from imp.remote.session.protocol import (
    CONTROL_SAVE,
    Attach,
    Control,
    Input,
    encode_attach,
    encode_control,
    encode_event,
    encode_input,
    parse_client_message,
    parse_event,
)


def test_encode_attach_is_single_line_kind_tagged_json() -> None:
    line = encode_attach("2026-06-15-abc", "Add dark mode")
    assert "\n" not in line
    assert json.loads(line) == {
        "kind": "attach",
        "change_id": "2026-06-15-abc",
        "title": "Add dark mode",
    }


def test_encode_input_is_single_line_kind_tagged_json() -> None:
    line = encode_input("yes, ship it")
    assert "\n" not in line
    assert json.loads(line) == {"kind": "input", "text": "yes, ship it"}


def test_encode_control_is_single_line_kind_tagged_json() -> None:
    line = encode_control("interrupt")
    assert "\n" not in line
    assert json.loads(line) == {"kind": "control", "action": "interrupt"}


def test_encode_control_rejects_unknown_action() -> None:
    with pytest.raises(ValueError, match="unknown control action"):
        encode_control("explode")


def test_encode_event_wraps_event_under_event_key() -> None:
    line = encode_event({"type": "agent_output", "payload": {"text": "hi"}})
    assert "\n" not in line
    assert json.loads(line) == {
        "kind": "event",
        "event": {"type": "agent_output", "payload": {"text": "hi"}},
    }


def test_parse_client_message_recovers_attach() -> None:
    assert parse_client_message(encode_attach("c1", "T")) == Attach("c1", "T")


def test_parse_client_message_recovers_input() -> None:
    assert parse_client_message(encode_input("reply")) == Input("reply")


def test_parse_client_message_recovers_control_for_each_action() -> None:
    for action in ("interrupt", "detach", "abandon", "save"):
        assert parse_client_message(encode_control(action)) == Control(action)


def test_parse_client_message_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown client message kind"):
        parse_client_message('{"kind": "bye"}')


def test_parse_client_message_rejects_unknown_control_action() -> None:
    with pytest.raises(ValueError, match="unknown control action"):
        parse_client_message('{"kind": "control", "action": "explode"}')


def test_parse_event_recovers_inner_event() -> None:
    inner = {"type": "step_started", "payload": {"step": "plan"}}
    assert parse_event(encode_event(inner)) == inner


def test_parse_event_rejects_non_event_frame() -> None:
    with pytest.raises(ValueError, match="expected event frame"):
        parse_event(encode_attach("c1", "T"))


def test_parse_client_message_rejects_non_object_json() -> None:
    for frame in ("42", '"hello"', "[1,2,3]", "null"):
        with pytest.raises(ValueError, match="expected JSON object"):
            parse_client_message(frame)


def test_parse_event_rejects_non_object_json() -> None:
    for frame in ("42", '"hello"', "[1,2,3]", "null"):
        with pytest.raises(ValueError, match="expected JSON object"):
            parse_event(frame)


def test_parse_event_rejects_non_object_event_field() -> None:
    with pytest.raises(ValueError, match="expected 'event' to be a JSON object"):
        parse_event('{"kind": "event", "event": 42}')


def test_encode_control_accepts_save_and_round_trips() -> None:
    line = encode_control(CONTROL_SAVE)
    assert json.loads(line) == {"kind": "control", "action": CONTROL_SAVE}
    assert parse_client_message(line) == Control(CONTROL_SAVE)

from __future__ import annotations

import json

import pytest

from imp.remote.session.protocol import (
    CONTROL_ARCHIVE,
    CONTROL_DISCARD,
    CONTROL_SAVE,
    Attach,
    Control,
    Input,
    SetLease,
    encode_attach,
    encode_control,
    encode_event,
    encode_input,
    encode_set_lease,
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
        "lease": False,
    }


def test_encode_attach_carries_lease_flag() -> None:
    leased = json.loads(encode_attach("c1", "T", lease=True))
    assert leased["lease"] is True
    unleased = json.loads(encode_attach("c1", "T"))
    assert unleased["lease"] is False


def test_parse_client_message_recovers_attach_lease() -> None:
    assert parse_client_message(
        encode_attach("c1", "T", lease=True)) == Attach("c1", "T", True)
    assert parse_client_message(
        encode_attach("c1", "T")) == Attach("c1", "T", False)


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
    for action in ("interrupt", "detach", "abandon", "save", "archive", "discard"):
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


def test_encode_control_accepts_archive_and_discard_and_round_trips() -> None:
    for action in (CONTROL_ARCHIVE, CONTROL_DISCARD):
        line = encode_control(action)
        assert json.loads(line) == {"kind": "control", "action": action}
        assert parse_client_message(line) == Control(action)


def test_encode_set_lease_round_trips() -> None:
    for value in (True, False):
        line = encode_set_lease(value)
        assert "\n" not in line
        assert json.loads(line) == {"kind": "set-lease", "value": value}
        assert parse_client_message(line) == SetLease(value)


def test_set_lease_missing_value_is_rejected() -> None:
    with pytest.raises(ValueError, match="missing required field"):
        parse_client_message('{"kind": "set-lease"}')


def test_interrupt_leave_control_round_trips() -> None:
    from imp.remote.session.protocol import CONTROL_INTERRUPT, CONTROL_INTERRUPT_LEAVE
    line = encode_control(CONTROL_INTERRUPT_LEAVE)
    assert parse_client_message(line) == Control(CONTROL_INTERRUPT_LEAVE)
    assert CONTROL_INTERRUPT_LEAVE != CONTROL_INTERRUPT  # distinct from plain interrupt


def test_control_reason_round_trips_with_and_without_reason() -> None:
    from imp.remote.session.protocol import CONTROL_INTERRUPT_LEAVE

    # With a reason: it survives the round trip as a distinct field.
    line = encode_control(CONTROL_INTERRUPT_LEAVE, "sigterm")
    assert "\n" not in line
    assert parse_client_message(line) == Control(CONTROL_INTERRUPT_LEAVE, "sigterm")

    # Without a reason: default None, and the wire omits the key (the omission is
    # action-agnostic — every reasonless control frame is byte-identical to before).
    line2 = encode_control(CONTROL_INTERRUPT_LEAVE)
    assert "reason" not in json.loads(line2)
    assert parse_client_message(line2) == Control(CONTROL_INTERRUPT_LEAVE, None)

    # Unknown reason strings are display/journal data, not dispatch: accepted verbatim.
    line3 = encode_control(CONTROL_INTERRUPT_LEAVE, "future-trigger")
    assert parse_client_message(line3).reason == "future-trigger"


def test_interrupt_force_control_round_trips() -> None:
    from imp.remote.session.protocol import (
        CONTROL_INTERRUPT_FORCE,
        Control,
        encode_control,
        parse_client_message,
    )

    assert CONTROL_INTERRUPT_FORCE == "interrupt-force"
    line = encode_control(CONTROL_INTERRUPT_FORCE)
    msg = parse_client_message(line)
    assert msg == Control(action="interrupt-force", reason=None)


def test_set_model_control_round_trips() -> None:
    from imp.remote.session.protocol import (
        CONTROL_SET_MODEL,
        Control,
        encode_control,
        parse_client_message,
    )

    assert CONTROL_SET_MODEL == "set-model"
    line = encode_control(CONTROL_SET_MODEL, "anthropic:claude-fable-5")
    assert parse_client_message(line) == Control(
        "set-model", "anthropic:claude-fable-5")

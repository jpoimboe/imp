from __future__ import annotations

import json

import pytest

from imp.remote.session.protocol import (
    _MAX_FRAME,
    CONTROL_ARCHIVE,
    CONTROL_DISCARD,
    CONTROL_SAVE,
    END_OF_RUN,
    SIDE_EFFECT_REASONS,
    Attach,
    Control,
    Input,
    SetLease,
    SkillRequest,
    encode_attach,
    encode_control,
    encode_end,
    encode_event,
    encode_input,
    encode_set_lease,
    encode_skill_request,
    parse_client_message,
    parse_event,
    parse_stream_frame,
)


def test_encode_attach_is_single_line_kind_tagged_json() -> None:
    line = encode_attach("2026-06-15-abc", "Add dark mode")
    assert "\n" not in line
    assert json.loads(line) == {
        "kind": "attach",
        "quest_id": "2026-06-15-abc",
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
    line = encode_event({"type": "agent_result", "payload": {"text": "hi"}})
    assert "\n" not in line
    assert json.loads(line) == {
        "kind": "event",
        "event": {"type": "agent_result", "payload": {"text": "hi"}},
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


def test_encode_skill_request_is_single_line_kind_tagged_json() -> None:
    line = encode_skill_request("refactor", "src/foo.py")
    assert "\n" not in line
    assert json.loads(line) == {
        "kind": "skill", "name": "refactor", "args": "src/foo.py"}


def test_parse_client_message_recovers_skill_request() -> None:
    assert parse_client_message(
        encode_skill_request("refactor", "src/foo.py")
    ) == SkillRequest("refactor", "src/foo.py")
    assert parse_client_message(
        encode_skill_request("debug", "")
    ) == SkillRequest("debug", "")


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


def test_encode_and_parse_set_sandbox_control() -> None:
    from imp.remote.session.protocol import (
        CONTROL_SET_SANDBOX,
        Control,
        encode_control,
        parse_client_message,
    )
    line = encode_control(CONTROL_SET_SANDBOX, "off")
    assert "\n" not in line
    assert parse_client_message(line) == Control(CONTROL_SET_SANDBOX, "off")


def test_side_effect_reasons_holds_model_override_and_compact() -> None:
    assert frozenset({"model-override", "compact"}) == SIDE_EFFECT_REASONS


def test_control_compact_is_registered_and_round_trips() -> None:
    from imp.remote.session.protocol import _CONTROL_ACTIONS, CONTROL_COMPACT

    assert CONTROL_COMPACT == "compact"
    assert CONTROL_COMPACT in _CONTROL_ACTIONS
    line = encode_control(CONTROL_COMPACT)
    assert "\n" not in line
    assert parse_client_message(line) == Control(CONTROL_COMPACT, None)


def test_retry_now_control_round_trips() -> None:
    from imp.remote.session.protocol import (
        CONTROL_RETRY_NOW,
        Control,
        encode_control,
        parse_client_message,
    )

    assert CONTROL_RETRY_NOW == "retry-now"
    line = encode_control(CONTROL_RETRY_NOW)
    assert "\n" not in line
    assert json.loads(line) == {"kind": "control", "action": CONTROL_RETRY_NOW}
    assert parse_client_message(line) == Control(CONTROL_RETRY_NOW, None)


def test_retry_now_unknown_action_still_rejected_at_both_layers() -> None:
    # The new action does not weaken validation: an unknown action still raises
    # at both the encode and the parse layer (the frozenset gate is unchanged).
    with pytest.raises(ValueError, match="unknown control action"):
        encode_control("explode")
    with pytest.raises(ValueError, match="unknown control action"):
        parse_client_message('{"kind": "control", "action": "explode"}')


def test_encode_event_clips_an_oversized_frame_to_the_wire_cap() -> None:
    huge = "x" * (2 << 20)
    event = {
        "type": "tool_call",
        "agent_id": "a1",
        "payload": {
            "id": "t1",
            "name": "write_file",
            "attempt": 3,
            "cached": False,
            "parent": None,
            "input": {"path": "big.txt", "content": huge},
            "tags": ["write", huge],
        },
    }
    frame = encode_event(event)
    assert len(frame.encode("utf-8")) + 1 <= _MAX_FRAME   # +1: the caller appends "\n"

    decoded = parse_event(frame)
    # every key survives, at its original nesting and in its original order
    assert list(decoded) == ["type", "agent_id", "payload"]
    assert list(decoded["payload"]) == [
        "id", "name", "attempt", "cached", "parent", "input", "tags"]
    assert list(decoded["payload"]["input"]) == ["path", "content"]
    # non-string leaves and short strings are carried through untouched
    assert decoded["type"] == "tool_call"
    assert decoded["agent_id"] == "a1"
    assert decoded["payload"]["attempt"] == 3
    assert decoded["payload"]["cached"] is False
    assert decoded["payload"]["parent"] is None
    assert decoded["payload"]["input"]["path"] == "big.txt"
    assert decoded["payload"]["tags"][0] == "write"
    # the long leaves are clipped in place and marked, keeping their head
    clipped = decoded["payload"]["input"]["content"]
    assert clipped.startswith("xxxx")
    assert clipped.endswith("… [clipped]")
    assert len(clipped) < len(huge)
    assert decoded["payload"]["tags"][1].endswith("… [clipped]")   # list leaves clip too


def test_encode_event_leaves_a_frame_that_fits_byte_identical() -> None:
    event = {"type": "agent_result", "payload": {"text": "hi", "n": 1}}
    frame = encode_event(event)
    assert frame == json.dumps({"kind": "event", "event": event})
    assert "clipped" not in frame


def test_encode_event_does_not_clip_a_large_frame_that_still_fits() -> None:
    text = "y" * (1 << 19)   # 512 KiB: large, still inside the 1 MiB frame budget
    frame = encode_event({"type": "agent_result", "payload": {"text": text}})
    assert len(frame.encode("utf-8")) + 1 <= _MAX_FRAME
    assert parse_event(frame)["payload"]["text"] == text


def test_encode_event_tightens_the_clip_until_many_strings_fit() -> None:
    # 8 x 256 KiB: no single string is over the cap, and one clipping pass at half
    # the budget still would not fit the frame — the clip has to keep tightening.
    event = {"type": "agent_result",
             "payload": {f"k{i}": "z" * (1 << 18) for i in range(8)}}
    frame = encode_event(event)
    assert len(frame.encode("utf-8")) + 1 <= _MAX_FRAME
    decoded = parse_event(frame)
    assert list(decoded["payload"]) == [f"k{i}" for i in range(8)]
    assert all(v.endswith("… [clipped]") for v in decoded["payload"].values())


def test_encode_end_is_a_bare_kind_tagged_frame() -> None:
    line = encode_end()
    assert "\n" not in line
    assert json.loads(line) == {"kind": "end"}      # no payload beyond its kind


def test_parse_stream_frame_recovers_the_end_marker() -> None:
    assert parse_stream_frame(encode_end()) is END_OF_RUN


def test_parse_stream_frame_recovers_an_inner_event() -> None:
    inner = {"type": "step_started", "payload": {"step": "plan"}}
    assert parse_stream_frame(encode_event(inner)) == inner


def test_parse_stream_frame_rejects_a_frame_that_is_neither() -> None:
    with pytest.raises(ValueError, match="expected event frame"):
        parse_stream_frame(encode_attach("c1", "T"))


def test_parse_event_still_rejects_the_end_frame() -> None:
    # The old-client degradation path: a CLI that predates the `end` kind resolves
    # the frame as a fault and takes its completed-or-lost-connection route.
    with pytest.raises(ValueError, match="expected event frame"):
        parse_event(encode_end())

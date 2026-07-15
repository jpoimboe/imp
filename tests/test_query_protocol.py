from __future__ import annotations

import pytest

from imp.remote.session.protocol import (
    Query,
    encode_query,
    encode_result,
    encode_result_error,
    parse_client_message,
    parse_result,
)


def test_query_frame_round_trips_through_parse_client_message() -> None:
    line = encode_query("get_events", {"quest_id": "abc"})
    assert parse_client_message(line) == Query("get_events", {"quest_id": "abc"})


def test_query_frame_defaults_missing_args_to_empty_dict() -> None:
    assert parse_client_message('{"kind": "query", "op": "completed_rows"}') == Query(
        "completed_rows", {})
    assert parse_client_message('{"kind": "query", "op": "completed_rows", "args": null}') == Query(
        "completed_rows", {})


def test_result_ok_frame_carries_value_through_parse_result() -> None:
    assert parse_result(encode_result([1, {"x": "y"}])) == [1, {"x": "y"}]


def test_result_ok_frame_may_carry_null_value() -> None:
    assert parse_result(encode_result(None)) is None


def test_result_error_frame_raises_with_message() -> None:
    with pytest.raises(RuntimeError, match="boom"):
        parse_result(encode_result_error("boom"))


def test_parse_result_rejects_a_non_result_frame() -> None:
    with pytest.raises(ValueError, match="expected result frame"):
        parse_result('{"kind": "event", "event": {}}')

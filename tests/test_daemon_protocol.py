from __future__ import annotations

import json

from imp.remote.daemon.protocol import make_hello, parse_hello


def test_make_hello_is_json_with_type_and_fields() -> None:
    obj = json.loads(make_hello("2026-06-13-abc", "Add dark mode"))
    assert obj == {
        "type": "hello",
        "change_id": "2026-06-13-abc",
        "title": "Add dark mode",
    }


def test_make_hello_is_a_single_line() -> None:
    assert "\n" not in make_hello("2026-06-13-abc", "Add dark mode")


def test_parse_hello_recovers_change_id_and_title() -> None:
    line = make_hello("2026-06-13-abc", "Add dark mode")
    assert parse_hello(line) == ("2026-06-13-abc", "Add dark mode")


def test_parse_hello_tolerates_empty_title() -> None:
    assert parse_hello(make_hello("2026-06-13-xyz", "")) == ("2026-06-13-xyz", "")


def test_parse_hello_works_on_literal_json() -> None:
    assert parse_hello('{"type": "hello", "change_id": "x", "title": "y"}') == ("x", "y")


def test_parse_hello_raises_on_wrong_type() -> None:
    import pytest

    with pytest.raises(ValueError, match="expected hello frame"):
        parse_hello('{"type": "bye", "change_id": "x", "title": "t"}')

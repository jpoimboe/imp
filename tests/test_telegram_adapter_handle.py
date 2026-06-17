from __future__ import annotations

from imp.remote.daemon.telegram_adapter import handle_for, parse_handle


def test_handle_for_returns_the_bare_id() -> None:
    assert handle_for("k3f9qa") == "k3f9qa"


def test_handle_for_returns_id_unchanged_for_any_string() -> None:
    assert handle_for("my-feature") == "my-feature"


def test_parse_handle_splits_a_leading_at_prefix_from_the_message() -> None:
    assert parse_handle("@k3f9qa do the thing") == ("k3f9qa", "do the thing")


def test_parse_handle_ignores_a_bare_handle_with_no_message() -> None:
    assert parse_handle("@k3f9qa") == (None, "@k3f9qa")


def test_parse_handle_returns_none_when_text_has_no_at_prefix() -> None:
    assert parse_handle("just a reply") == (None, "just a reply")

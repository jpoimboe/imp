from __future__ import annotations

from imp.remote.daemon.telegram_adapter import handle_for, parse_handle


def test_handle_for_is_the_change_id_suffix() -> None:
    assert handle_for("2026-06-13-k3f9qa") == "k3f9qa"


def test_handle_for_passes_through_an_id_without_a_dash() -> None:
    assert handle_for("solo") == "solo"


def test_handle_for_returns_full_id_for_a_hyphenated_legacy_slug() -> None:
    assert handle_for("2026-06-13-my-feature") == "2026-06-13-my-feature"


def test_parse_handle_splits_a_leading_at_prefix_from_the_message() -> None:
    assert parse_handle("@k3f9qa do the thing") == ("k3f9qa", "do the thing")


def test_parse_handle_handles_a_bare_handle_with_no_message() -> None:
    assert parse_handle("@k3f9qa") == ("k3f9qa", "")


def test_parse_handle_returns_none_when_text_has_no_at_prefix() -> None:
    assert parse_handle("just a reply") == (None, "just a reply")

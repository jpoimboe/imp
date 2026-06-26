from __future__ import annotations

from imp.remote.daemon.commands import (
    ABANDON_USAGE,
    NEW_USAGE,
    RESUME_USAGE,
    ROUTE_HINT,
    SEARCH_USAGE,
    UNKNOWN_COMMAND,
    Abandon,
    Create,
    Help,
    ListChanges,
    Notice,
    Reply,
    Resume,
    Save,
    Search,
    parse,
)
from imp.remote.daemon.telegram_adapter import parse_handle

# A fake resolve_lookup: reply_to 42 -> change A; @good -> change B; else unresolved.
A = "2026-06-13-aaaaaa"
B = "2026-06-13-bbbbbb"


def fake_resolve(text: str, reply_to: int | None) -> tuple[str | None, str]:
    if reply_to == 42:
        return (A, text)
    handle, rest = parse_handle(text)
    if handle == "good":
        return (B, rest)
    return (None, text)


def test_slash_show_is_listchanges() -> None:
    assert parse("/show", None, fake_resolve) == ListChanges()


def test_leading_whitespace_still_selects_the_command_path() -> None:
    assert parse("   /show", None, fake_resolve) == ListChanges()


def test_slash_help_is_help() -> None:
    assert parse("/help", None, fake_resolve) == Help()


def test_slash_new_with_prompt_is_create() -> None:
    assert parse("/new add a foo() helper", None, fake_resolve) == Create(
        "add a foo() helper")


def test_slash_new_empty_is_a_usage_notice() -> None:
    assert parse("/new   ", None, fake_resolve) == Notice(NEW_USAGE)


def test_slash_abandon_reply_to_resolves_the_target() -> None:
    assert parse("/abandon", 42, fake_resolve) == Abandon(A)


def test_slash_abandon_at_handle_resolves_the_target() -> None:
    assert parse("/abandon @good", None, fake_resolve) == Abandon(B)


def test_slash_abandon_untargeted_is_a_usage_notice() -> None:
    assert parse("/abandon", None, fake_resolve) == Notice(ABANDON_USAGE)


def test_slash_abandon_unresolvable_handle_is_a_usage_notice() -> None:
    assert parse("/abandon @nope", None, fake_resolve) == Notice(ABANDON_USAGE)


def test_slash_ls_is_no_longer_an_alias_for_listchanges() -> None:
    result = parse("/ls", None, fake_resolve)
    assert result != ListChanges()
    assert result == Notice(UNKNOWN_COMMAND)


def test_unknown_command_points_at_help() -> None:
    assert parse("/frobnicate", None, fake_resolve) == Notice(UNKNOWN_COMMAND)


def test_non_slash_resolving_is_a_reply() -> None:
    assert parse("approved", 42, fake_resolve) == Reply(A, "approved")


def test_non_slash_at_handle_is_a_reply_with_stripped_text() -> None:
    assert parse("@good ship it", None, fake_resolve) == Reply(B, "ship it")


def test_non_slash_unresolvable_is_a_route_hint() -> None:
    assert parse("who is this", None, fake_resolve) == Notice(ROUTE_HINT)


def test_bare_at_handle_with_no_text_is_a_route_hint() -> None:
    assert parse("@good", None, fake_resolve) == Notice(ROUTE_HINT)


def test_slash_search_with_query_is_a_search() -> None:
    assert parse("/search dark mode", None, fake_resolve) == Search("dark mode")


def test_slash_search_empty_is_a_usage_notice() -> None:
    assert parse("/search   ", None, fake_resolve) == Notice(SEARCH_USAGE)


def test_slash_resume_with_token_is_a_resume() -> None:
    assert parse("/resume 2026-06-17", None, fake_resolve) == Resume("2026-06-17")


def test_slash_resume_empty_is_a_usage_notice() -> None:
    assert parse("/resume   ", None, fake_resolve) == Notice(RESUME_USAGE)


def test_slash_save_reply_to_resolves_the_target() -> None:
    assert parse("/save", 42, fake_resolve) == Save(A)


def test_slash_save_at_handle_resolves_the_target() -> None:
    assert parse("/save @good", None, fake_resolve) == Save(B)


def test_slash_save_untargeted_carries_no_change_id() -> None:
    assert parse("/save", None, fake_resolve) == Save(None)

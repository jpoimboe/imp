from __future__ import annotations

from imp.remote.daemon.commands import (
    HELP_TEXT,
    NEW_USAGE,
    RESTART_CONFIRMATION,
    RESUME_USAGE,
    ROUTE_HINT,
    UNKNOWN_COMMAND,
    Alerts,
    Create,
    Help,
    ListQuests,
    ListResult,
    Notice,
    Reply,
    Restart,
    Resume,
    parse,
)
from imp.remote.daemon.discover import QuestInfo
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


def test_slash_list_is_listquests() -> None:
    assert parse("/list", None, fake_resolve) == ListQuests()


def test_slash_show_is_no_longer_recognized() -> None:
    assert parse("/show", None, fake_resolve) == Notice(UNKNOWN_COMMAND)


def test_leading_whitespace_still_selects_the_command_path() -> None:
    assert parse("   /list", None, fake_resolve) == ListQuests()


def test_slash_help_is_help() -> None:
    assert parse("/help", None, fake_resolve) == Help()


def test_slash_new_with_prompt_is_create() -> None:
    assert parse("/new add a foo() helper", None, fake_resolve) == Create(
        "add a foo() helper")


def test_slash_new_empty_is_a_usage_notice() -> None:
    assert parse("/new   ", None, fake_resolve) == Notice(NEW_USAGE)


def test_slash_abandon_is_no_longer_recognized() -> None:
    assert parse("/abandon @good", None, fake_resolve) == Notice(UNKNOWN_COMMAND)


def test_help_text_no_longer_mentions_abandon() -> None:
    assert "/abandon" not in HELP_TEXT


def test_slash_ls_is_no_longer_an_alias_for_listquests() -> None:
    result = parse("/ls", None, fake_resolve)
    assert result != ListQuests()
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


def test_slash_search_is_no_longer_recognized() -> None:
    assert parse("/search dark mode", None, fake_resolve) == Notice(UNKNOWN_COMMAND)
    assert parse("/search   ", None, fake_resolve) == Notice(UNKNOWN_COMMAND)


def test_help_text_no_longer_mentions_search() -> None:
    assert "/search" not in HELP_TEXT


def test_slash_resume_with_token_is_a_resume() -> None:
    assert parse("/resume 2026-06-17", None, fake_resolve) == Resume("2026-06-17")


def test_slash_resume_empty_is_a_usage_notice() -> None:
    assert parse("/resume   ", None, fake_resolve) == Notice(RESUME_USAGE)


def test_slash_save_is_no_longer_recognized() -> None:
    assert parse("/save @good", None, fake_resolve) == Notice(UNKNOWN_COMMAND)


def test_help_text_no_longer_mentions_save() -> None:
    assert "/save" not in HELP_TEXT


def test_listresult_defaults_resident_to_empty_frozenset() -> None:
    result = ListResult([QuestInfo(id="a", title="A", status="running", step="plan")])
    assert result.resident == frozenset()


def test_listresult_carries_the_resident_set() -> None:
    result = ListResult([], frozenset({"a", "b"}))
    assert result.resident == frozenset({"a", "b"})


def test_list_help_line_says_running_quests() -> None:
    assert "/list — show running quests (● = subscribed)" in HELP_TEXT


def test_resume_help_line_says_paused_or_archived() -> None:
    assert ("/resume <id or text> — rehydrate and continue a paused/archived "
            "conversation\n") in HELP_TEXT


def test_route_hint_points_at_list_not_show() -> None:
    assert "send /list." in ROUTE_HINT
    assert "/show" not in ROUTE_HINT


def test_slash_alerts_on_parses_to_alerts_true() -> None:
    assert parse("/alerts on", None, fake_resolve) == Alerts(True)


def test_slash_alerts_off_parses_to_alerts_false() -> None:
    assert parse("/alerts off", None, fake_resolve) == Alerts(False)


def test_slash_alerts_bare_parses_to_alerts_none() -> None:
    assert parse("/alerts", None, fake_resolve) == Alerts(None)


def test_slash_alerts_unknown_arg_parses_to_alerts_none() -> None:
    assert parse("/alerts maybe", None, fake_resolve) == Alerts(None)


def test_slash_alerts_arg_is_case_insensitive() -> None:
    assert parse("/alerts ON", None, fake_resolve) == Alerts(True)


def test_help_text_mentions_alerts() -> None:
    assert (
        "/alerts on|off — alert on every quest, not just the ones you follow (default: on)"
        in HELP_TEXT)


def test_help_text_mentions_subscribe_streams_from_now() -> None:
    assert (
        "/subscribe @handle — follow a quest (streams from now; /replay for history)"
        in HELP_TEXT)


def test_slash_restart_parses_to_restart() -> None:
    assert parse("/restart", None, fake_resolve) == Restart()


def test_slash_restart_with_trailing_text_still_parses_to_restart() -> None:
    assert parse("/restart now please", None, fake_resolve) == Restart()


def test_help_text_mentions_restart() -> None:
    assert "/restart — reload the daemon to run the latest code" in HELP_TEXT


def test_restart_confirmation_copy() -> None:
    assert RESTART_CONFIRMATION == "Restarted — now running the latest code."

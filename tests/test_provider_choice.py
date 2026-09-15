from __future__ import annotations

from imp.providers import BUILTIN_FLOOR, provider_choice_error, selectable_providers
from imp.providers.resolve import provider_is_available


def test_selectable_is_the_floor_minus_the_entry_with_no_tier_table() -> None:
    # `scripted` would pass a bare name check and then die at resolve with a
    # LoadError, so it is excluded at the door rather than validated twice.
    assert selectable_providers() == ["anthropic", "openai", "claude", "codex"]
    assert BUILTIN_FLOOR["scripted"].tier_defaults is None


def test_availability_is_asked_per_name_not_through_a_config_list(
        floor_availability) -> None:
    # --provider deliberately reaches a vendor the configured order never names,
    # so this must not filter through config.providers the way /model does.
    floor_availability({"codex"})

    assert provider_is_available("codex") is True
    assert provider_is_available("anthropic") is False
    assert provider_is_available("nope") is False


def test_an_unknown_name_names_both_groups_in_floor_order(floor_availability) -> None:
    floor_availability({"claude", "codex"})

    assert provider_choice_error("foo") == (
        "unknown provider 'foo' (available here: claude, codex; "
        "needs setup: anthropic, openai)")


def test_the_needs_setup_clause_disappears_when_nothing_needs_setup(
        floor_availability) -> None:
    # No empty group and no trailing "; " — the clause is omitted entirely.
    floor_availability({"anthropic", "openai", "claude", "codex"})

    assert provider_choice_error("foo") == (
        "unknown provider 'foo' "
        "(available here: anthropic, openai, claude, codex)")


def test_scripted_is_refused_by_the_unknown_provider_path(floor_availability) -> None:
    floor_availability({"claude", "codex"})

    assert provider_choice_error("scripted") == (
        "unknown provider 'scripted' (available here: claude, codex; "
        "needs setup: anthropic, openai)")


def test_an_unavailable_selectable_name_gets_that_vendors_own_auth_hint(
        floor_availability) -> None:
    # Two different vendors, two different hints: the line is composed from the
    # floor's auth_hint field, not written literally for one of them.
    floor_availability({"claude"})

    assert provider_choice_error("codex") == (
        "codex is not available — run `codex login`")
    assert provider_choice_error("openai") == (
        "openai is not available — set OPENAI_API_KEY")


def test_a_selectable_available_name_raises_no_objection(floor_availability) -> None:
    floor_availability({"claude", "codex"})

    assert provider_choice_error("codex") is None
    assert provider_choice_error("claude") is None

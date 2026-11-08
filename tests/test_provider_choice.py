from __future__ import annotations

import inspect

from imp.providers import (
    BUILTIN_FLOOR,
    ProviderGroups,
    provider_choice_error,
    provider_groups,
    selectable_providers,
)
from imp.providers.config import Config
from imp.providers.resolve import provider_is_available

_CLAUDE = Config(providers=("claude",))


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


def test_groups_partition_the_selectable_set(floor_availability) -> None:
    floor_availability({"claude", "codex"})

    assert provider_groups(_CLAUDE) == ProviderGroups(
        configured=("claude",), also_installed=("codex",),
        needs_setup=("anthropic", "openai"))


def test_configured_follows_the_providers_order_not_the_floor(
        floor_availability) -> None:
    floor_availability({"claude", "codex"})

    groups = provider_groups(Config(providers=("codex", "claude")))

    assert groups.configured == ("codex", "claude")
    assert groups.also_installed == ()


def test_a_configured_vendor_that_is_not_installed_lands_in_needs_setup(
        floor_availability) -> None:
    floor_availability({"codex"})

    assert provider_groups(_CLAUDE) == ProviderGroups(
        configured=(), also_installed=("codex",),
        needs_setup=("anthropic", "openai", "claude"))


def test_a_configured_name_with_no_tier_table_is_never_grouped(
        floor_availability) -> None:
    floor_availability({"scripted", "claude"})

    groups = provider_groups(Config(providers=("scripted", "claude")))

    assert groups.configured == ("claude",)
    assert "scripted" not in (*groups.also_installed, *groups.needs_setup)


def test_an_unknown_name_names_the_three_groups(floor_availability) -> None:
    floor_availability({"claude", "codex"})

    assert provider_choice_error("foo", _CLAUDE) == (
        "unknown provider 'foo' (configured: claude; also installed: codex; "
        "needs setup: anthropic, openai)")


def test_an_empty_also_installed_group_is_omitted(floor_availability) -> None:
    floor_availability({"claude", "codex"})

    assert provider_choice_error("foo", Config(providers=("claude", "codex"))) == (
        "unknown provider 'foo' (configured: claude, codex; "
        "needs setup: anthropic, openai)")


def test_empty_configured_and_needs_setup_groups_are_omitted(
        floor_availability) -> None:
    # No empty group and no stray "; " — each empty segment is dropped whole.
    floor_availability({"anthropic", "openai", "claude", "codex"})

    assert provider_choice_error("foo", Config()) == (
        "unknown provider 'foo' (also installed: anthropic, openai, claude, codex)")


def test_scripted_is_refused_by_the_unknown_provider_path(floor_availability) -> None:
    floor_availability({"claude", "codex"})

    assert provider_choice_error("scripted", _CLAUDE) == (
        "unknown provider 'scripted' (configured: claude; also installed: codex; "
        "needs setup: anthropic, openai)")


def test_an_unavailable_selectable_name_gets_that_vendors_own_auth_hint(
        floor_availability) -> None:
    # Two different vendors, two different hints: the line is composed from the
    # floor's auth_hint field, not written literally for one of them.
    floor_availability({"claude"})

    assert provider_choice_error("codex", _CLAUDE) == (
        "codex is not available — run `codex login`")
    assert provider_choice_error("openai", _CLAUDE) == (
        "openai is not available — set OPENAI_API_KEY")


def test_a_configured_but_uninstalled_name_gets_its_auth_hint(
        floor_availability) -> None:
    floor_availability({"codex"})

    assert provider_choice_error("claude", _CLAUDE) == (
        "claude is not available — run `claude login`")


def test_an_installed_name_is_accepted_whether_or_not_it_is_configured(
        floor_availability) -> None:
    # The escape hatch: a pin may reach an installed vendor `providers:` never names.
    floor_availability({"claude", "codex"})

    assert provider_choice_error("claude", _CLAUDE) is None
    assert provider_choice_error("codex", _CLAUDE) is None


def test_the_choice_error_reads_availability_only_through_the_groups() -> None:
    assert "provider_is_available" not in inspect.getsource(provider_choice_error)

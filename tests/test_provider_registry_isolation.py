"""The autouse net in conftest.py that makes the module-global provider
registry a per-test resource.

The two tests below are one regression test in two halves: the first leaves the
registry holding only a fake (the unbalanced reset-and-register shape the ~126
in-test call sites use), the second asserts the next test does not inherit it.
This module must never grow a fixture that touches the registry — that would
isolate these tests from the net they exist to pin. xdist_group keeps both
halves on one worker, in collection order, under `-n auto --dist loadgroup`.
"""
import pytest

from imp.providers import resolve


@pytest.mark.xdist_group("provider_registry_net")
def test_a_test_may_leave_the_registry_holding_only_a_fake() -> None:
    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "housecat", env_check=lambda: True, factory=lambda model: object())
    assert sorted(resolve._PROVIDERS) == ["housecat"]


@pytest.mark.xdist_group("provider_registry_net")
def test_the_next_test_starts_from_an_empty_registry() -> None:
    """conftest's autouse _reset_provider_registry cleared the fake the previous
    test left behind, so _ensure_builtins() reloads the real providers on demand."""
    assert resolve._PROVIDERS == {}

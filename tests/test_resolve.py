# tests/test_resolve.py
from __future__ import annotations

import pytest

from imp.providers.config import ProviderModel, RoleConfig
from imp.providers.resolve import (
    ProviderUnavailable,
    UnknownProvider,
    register_provider,
    resolve_role,
)


class _FakeProvider:
    name = "fake"

    def __init__(self, model: str) -> None:
        self.model_name = model

    def create_runner(self, invocation):
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _register_fake(monkeypatch):
    # Clean slate per test
    from imp.providers.resolve import reset_providers_for_tests
    reset_providers_for_tests()
    register_provider(
        "fake_present",
        env_check=lambda: True,
        factory=lambda model: _FakeProvider(model),
    )
    register_provider(
        "fake_absent",
        env_check=lambda: False,
        factory=lambda model: _FakeProvider(model),
    )


def test_resolves_first_available() -> None:
    role = RoleConfig(
        name="default",
        prefer=(
            ProviderModel(provider="fake_absent", model="m1"),
            ProviderModel(provider="fake_present", model="m2"),
        ),
    )
    p = resolve_role(role)
    assert isinstance(p, _FakeProvider)
    assert p.model_name == "m2"


def test_unknown_provider_raises() -> None:
    role = RoleConfig(name="r", prefer=(ProviderModel(provider="nobody", model="m"),))
    with pytest.raises(UnknownProvider):
        resolve_role(role)


def test_unavailable_raises_when_nothing_resolves() -> None:
    role = RoleConfig(name="r", prefer=(ProviderModel(provider="fake_absent", model="m"),))
    with pytest.raises(ProviderUnavailable):
        resolve_role(role)


def test_anthropic_registered_when_imported(monkeypatch):
    from imp.providers import resolve
    resolve.reset_providers_for_tests()
    import importlib

    import imp.providers as pkg
    importlib.reload(pkg)  # triggers eager registrations

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    role = RoleConfig(
        name="default",
        prefer=(ProviderModel(provider="anthropic", model="claude-sonnet-4-6"),),
    )
    p = resolve_role(role)
    assert p.name == "anthropic"

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


def test_anthropic_and_openai_both_registered_after_import() -> None:
    """providers/__init__ must register both new V1 providers eagerly."""
    import importlib
    import os

    import imp.providers
    from imp.providers import resolve

    # Reload to flush any test-time mutation, then RE-EXECUTE the package's
    # top-level code so register_provider(...) calls fire again. NOTE:
    # importlib.import_module returns the cached module without re-running
    # top-level — must use importlib.reload here.
    resolve.reset_providers_for_tests()
    importlib.reload(imp.providers)

    # Both providers should be registered now.
    role_anthropic = RoleConfig(
        name="r",
        prefer=(ProviderModel(provider="anthropic", model="claude-sonnet-4-6"),),
    )
    role_openai = RoleConfig(
        name="r",
        prefer=(ProviderModel(provider="openai", model="gpt-4o-mini"),),
    )

    if os.environ.get("ANTHROPIC_API_KEY"):
        provider = resolve_role(role_anthropic)
        assert provider.name == "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        provider = resolve_role(role_openai)
        assert provider.name == "openai"

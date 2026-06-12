from __future__ import annotations

import pytest

from imp.providers import resolve, scripted
from imp.providers.config import Config


@pytest.fixture(autouse=True)
def _real_registry():
    # Restore the global builtin registry (some tests clear it). The autouse
    # _stub_provider_api_keys fixture (conftest) sets ANTHROPIC_API_KEY=sk-test,
    # so anthropic._env_check() is True in this test.
    from imp.providers import register_builtin_providers
    resolve.reset_providers_for_tests()
    register_builtin_providers()
    yield
    resolve.reset_providers_for_tests()
    register_builtin_providers()


def test_scripted_is_registered_as_a_builtin():
    assert "scripted" in resolve._PROVIDERS


def test_scripted_env_check_is_gated_on_the_script_env_var(monkeypatch):
    monkeypatch.delenv(scripted.ENV_VAR, raising=False)
    assert scripted._env_check() is False
    monkeypatch.setenv(scripted.ENV_VAR, "/tmp/script.json")
    assert scripted._env_check() is True


def test_registering_scripted_does_not_contaminate_production_resolution(monkeypatch):
    # scripted is registered, but a config naming only anthropic must still
    # resolve to anthropic — the existing selection path is unaffected.
    monkeypatch.delenv(scripted.ENV_VAR, raising=False)
    cfg = Config(providers=("anthropic",))
    assert resolve._first_available_provider(cfg) == "anthropic"

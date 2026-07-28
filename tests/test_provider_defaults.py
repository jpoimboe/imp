"""Provider registry surface: each provider module declares a TIER_DEFAULTS table
covering low/medium/high, and the builtins register lazily on first registry read."""
import importlib
from collections.abc import Callable, Generator
from typing import ClassVar

import pytest

from imp.providers import register_builtin_providers, resolve
from imp.providers.config import ProviderModel

_MODULES = ["anthropic", "openai", "claude_code", "codex"]
_REQUIRED = {"low", "medium", "high"}
_BUILTINS = ["anthropic", "claude_code", "codex", "openai", "scripted"]


class _Cfg:
    """Minimal stand-in for providers.config.Config: the two registry readers that
    take a config only ever walk `.providers`."""
    providers: ClassVar[tuple[str, ...]] = ("anthropic",)


@pytest.mark.parametrize("mod_name", _MODULES)
def test_tier_defaults_cover_required_tiers(mod_name: str) -> None:
    mod = importlib.import_module(f"imp.providers.{mod_name}")
    assert set(mod.TIER_DEFAULTS) >= _REQUIRED
    for tier in _REQUIRED:
        assert isinstance(mod.TIER_DEFAULTS[tier], str)
        assert mod.TIER_DEFAULTS[tier]


def test_env_var_present_for_http_providers() -> None:
    import imp.providers.anthropic as a
    import imp.providers.openai as o
    assert a.ENV_VAR == "ANTHROPIC_API_KEY"
    assert o.ENV_VAR == "OPENAI_API_KEY"


def test_env_var_none_for_cli_providers() -> None:
    import imp.providers.claude_code as c
    import imp.providers.codex as x
    assert c.ENV_VAR is None
    assert x.ENV_VAR is None


# These tests mutate the module-global registry. An autouse fixture resets it
# before AND restores the builtins after EACH test, so order-dependence can't
# bite if a test is later inserted between them (finding L3 — parity with
# test_effective_config.py). The import-only tests above are unaffected.
# Annotated as a Generator (not `-> None`) because it yields — `-> None` with a
# yield fails mypy ("return type should be Generator").
@pytest.fixture(autouse=True)
def _registry() -> Generator[None, None, None]:
    resolve.reset_providers_for_tests()
    register_builtin_providers()
    yield
    resolve.reset_providers_for_tests()
    register_builtin_providers()


def test_iter_provider_defaults_includes_builtins() -> None:
    defaults = resolve.iter_provider_defaults()
    assert {"anthropic", "openai", "claude_code", "codex"} <= set(defaults)
    anthropic_tiers = defaults["anthropic"].tier_defaults
    assert anthropic_tiers is not None
    assert anthropic_tiers["high"] == "claude-opus-5[1m]"
    assert defaults["openai"].env_var == "OPENAI_API_KEY"
    assert defaults["claude_code"].env_var is None


def test_partial_tier_defaults_rejected_at_registration() -> None:
    resolve.reset_providers_for_tests()
    with pytest.raises(ValueError, match="must cover"):
        resolve.register_provider(
            "broken", env_check=lambda: True, factory=lambda m: None,  # type: ignore[arg-type,return-value]
            tier_defaults={"low": "x"},  # missing medium/high
        )
    # autouse teardown restores the builtins — no manual re-register needed.


def test_unpacking_sites_still_two_tuple() -> None:
    env_check, factory = resolve._PROVIDERS["openai"]  # must still be a 2-tuple
    assert callable(env_check) and callable(factory)


# Each reader repopulates from EMPTY, so this also pins that
# reset_providers_for_tests() self-heals — the property a boolean
# "already loaded" flag would break for tests that install fake providers.
@pytest.mark.parametrize(
    "read",
    [
        pytest.param(lambda: resolve.iter_provider_defaults(), id="iter_provider_defaults"),
        pytest.param(lambda: resolve.available_providers(_Cfg()), id="available_providers"),
        pytest.param(
            lambda: resolve._first_available_provider(_Cfg()), id="_first_available_provider"
        ),
        pytest.param(
            lambda: resolve.provider_from_model(
                ProviderModel(provider="scripted", model="m")
            ),
            id="provider_from_model",
        ),
    ],
)
def test_registry_reader_repopulates_an_empty_registry(read: Callable[[], object]) -> None:
    resolve.reset_providers_for_tests()
    assert resolve._PROVIDERS == {}
    read()
    assert sorted(resolve._PROVIDERS) == _BUILTINS

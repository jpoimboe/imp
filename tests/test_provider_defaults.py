"""Each provider module declares a TIER_DEFAULTS table covering low/medium/high."""
import importlib

import pytest

_MODULES = ["anthropic", "openai", "claude_code", "codex"]
_REQUIRED = {"low", "medium", "high"}


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

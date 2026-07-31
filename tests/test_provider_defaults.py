"""Provider registry surface: each provider module declares a TIER_DEFAULTS table
covering low/medium/high, and the builtins register lazily on first registry read."""
import importlib
import subprocess
import sys
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


def test_importing_the_package_registers_nothing() -> None:
    """Fresh interpreter: package import and first registry read must not load provider modules."""
    code = (
        "import sys, imp.providers\n"
        "from imp.providers import resolve\n"
        "PROVIDER_MODULES = {\n"
        "    'imp.providers.anthropic', 'imp.providers.openai',\n"
        "    'imp.providers.claude_code', 'imp.providers.codex',\n"
        "    'imp.providers.codex_runner', 'imp.providers.scripted'}\n"
        "def leaked():\n"
        "    out = sorted(n for n in sys.modules if n in PROVIDER_MODULES)\n"
        "    if 'httpx' in sys.modules:\n"
        "        out.append('httpx')\n"
        "    return ','.join(out)\n"
        "print('EAGER:' + leaked())\n"
        "print('BEFORE:' + ','.join(sorted(resolve._PROVIDERS)))\n"
        "resolve.iter_provider_defaults()\n"
        "print('AFTER:' + ','.join(sorted(resolve._PROVIDERS)))\n"
        "print('LEAKED:' + leaked())\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    reported = {
        key: value
        for key, _, value in (ln.partition(":") for ln in proc.stdout.splitlines())
        if key in {"EAGER", "BEFORE", "AFTER", "LEAKED"}
    }
    assert reported.keys() == {"EAGER", "BEFORE", "AFTER", "LEAKED"}, (
        f"child did not report all four lines; stdout was {proc.stdout!r}"
    )
    assert reported["EAGER"] == "", f"import imp.providers eagerly loaded: {reported['EAGER']}"
    assert reported["BEFORE"] == ""
    assert reported["AFTER"] == ",".join(_BUILTINS)
    assert reported["LEAKED"] == "", (
        f"the first registry read imported: {reported['LEAKED']}"
    )


# Exact literals — a wrong-but-plausible id ships a bad default with a green suite.
_EXPECTED_FLOOR: dict[str, tuple[dict[str, str] | None, str | None]] = {
    "anthropic": (
        {
            "low": "claude-haiku-4-5",
            "medium": "claude-sonnet-4-6",
            "high": "claude-opus-5[1m]",
        },
        "ANTHROPIC_API_KEY",
    ),
    "openai": (
        {"low": "gpt-5.4-mini", "medium": "gpt-5.4", "high": "gpt-5.5"},
        "OPENAI_API_KEY",
    ),
    "claude_code": (
        {
            "low": "claude-haiku-4-5",
            "medium": "claude-sonnet-4-6",
            "high": "claude-opus-5[1m]",
        },
        None,
    ),
    "codex": (
        {"low": "gpt-5.4-mini", "medium": "gpt-5.4", "high": "gpt-5.5"},
        None,
    ),
    "scripted": (None, "IMP_SCRIPTED_SCRIPT"),
}


def test_iter_provider_defaults_returns_the_whole_builtin_floor() -> None:
    defaults = resolve.iter_provider_defaults()
    assert sorted(defaults) == _BUILTINS
    actual = {n: (d.tier_defaults, d.env_var) for n, d in defaults.items()}
    assert actual == _EXPECTED_FLOOR
    for name in _MODULES:
        tiers = defaults[name].tier_defaults
        assert tiers is not None, f"{name} contributes no floor tiers"
        assert set(tiers) >= _REQUIRED


def test_registry_factory_builds_the_providers_own_provider() -> None:
    from imp.providers.scripted import ScriptedProvider
    prov = resolve.provider_from_model(ProviderModel(provider="scripted", model="m"))
    assert isinstance(prov, ScriptedProvider)
    assert prov.model == "m"


def test_registered_fake_floor_reaches_effective_config_and_render() -> None:
    """A registered fake's tier_defaults must reach the floor layer and `imp config`'s rendering."""
    import io

    from imp.cli.config_cmd import render_config
    from imp.providers.effective import load_effective_config

    resolve.register_provider(
        "faux", env_check=lambda: True, factory=lambda m: None,  # type: ignore[arg-type,return-value]
        tier_defaults={"low": "f-lo", "medium": "f-mid", "high": "f-hi"},
        env_var="FAUX_KEY",
    )
    eff = load_effective_config()
    assert eff.config.tiers["high"]["faux"] == "f-hi"
    assert eff.config.tiers["low"]["faux"] == "f-lo"

    out = io.StringIO()
    render_config(eff, out, state_dir=None, color=False, show_all=True)
    row = next(
        (ln for ln in out.getvalue().splitlines() if ln.strip().startswith("high faux ")),
        None,
    )
    assert row is not None, out.getvalue()
    assert "f-hi" in row
    assert "provider default" in row


def test_registry_env_check_follows_a_module_level_monkeypatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thunked env_check resolves per call, so a module monkeypatch is honored."""
    import imp.providers.anthropic as a

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env_check, _factory = resolve._PROVIDERS["anthropic"]
    assert env_check() is False
    monkeypatch.setattr(a, "_env_check", lambda: True)
    assert env_check() is True


def test_builtin_floor_table_names_a_module_for_every_builtin() -> None:
    """Every BUILTIN_FLOOR entry names a real module exposing _env_check and _factory."""
    from imp.providers import BUILTIN_FLOOR

    assert sorted(BUILTIN_FLOOR) == _BUILTINS
    for name, entry in BUILTIN_FLOOR.items():
        mod = importlib.import_module(f"imp.providers.{entry.module}")
        assert callable(mod._env_check), name
        assert callable(mod._factory), name

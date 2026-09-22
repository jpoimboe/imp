"""Provider registry surface: imp.providers.BUILTIN_FLOOR is the single floor
table — each builtin's tier_defaults (low/medium/high for the model-bearing
providers) and env_var — and the builtins register lazily on first registry read."""
import importlib
import subprocess
import sys
from collections.abc import Callable, Generator
from typing import ClassVar

import pytest

from imp.providers import BUILTIN_FLOOR, register_builtin_providers, resolve
from imp.providers.config import ProviderModel

_MODEL_BEARING = ["anthropic", "openai", "claude", "codex"]
_REQUIRED = {"low", "medium", "high"}
_BUILTINS = ["anthropic", "claude", "codex", "openai", "scripted"]


class _Cfg:
    """Minimal stand-in for providers.config.Config: the two registry readers that
    take a config only ever walk `.providers`."""
    providers: ClassVar[tuple[str, ...]] = ("anthropic",)


@pytest.mark.parametrize("name", _MODEL_BEARING)
def test_tier_defaults_cover_required_tiers(name: str) -> None:
    tiers = BUILTIN_FLOOR[name].tier_defaults
    assert tiers is not None
    assert set(tiers) >= _REQUIRED
    for tier in _REQUIRED:
        assert isinstance(tiers[tier], str)
        assert tiers[tier]


def test_env_var_present_for_http_providers() -> None:
    assert BUILTIN_FLOOR["anthropic"].env_var == "ANTHROPIC_API_KEY"
    assert BUILTIN_FLOOR["openai"].env_var == "OPENAI_API_KEY"


def test_env_var_none_for_cli_providers() -> None:
    assert BUILTIN_FLOOR["claude"].env_var is None
    assert BUILTIN_FLOOR["codex"].env_var is None


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
        "    'imp.providers.claude', 'imp.providers.codex',\n"
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
            "high": "claude-opus-5-5",
        },
        "ANTHROPIC_API_KEY",
    ),
    "openai": (
        {"low": "gpt-5.6-luna", "medium": "gpt-5.6-terra", "high": "gpt-5.6-sol"},
        "OPENAI_API_KEY",
    ),
    "claude": (
        {
            "low": "claude-haiku-4-5",
            "medium": "claude-sonnet-4-6",
            "high": "claude-opus-5-5",
        },
        None,
    ),
    "codex": (
        {
            "low": "gpt-5.6-luna",
            "medium": "gpt-5.6-terra",
            "high": "gpt-5.6-sol",
        },
        None,
    ),
    "scripted": (None, "IMP_SCRIPTED_SCRIPT"),
}


def test_iter_provider_defaults_returns_the_whole_builtin_floor() -> None:
    defaults = resolve.iter_provider_defaults()
    assert sorted(defaults) == _BUILTINS
    actual = {n: (d.tier_defaults, d.env_var) for n, d in defaults.items()}
    assert actual == _EXPECTED_FLOOR
    for name in _MODEL_BEARING:
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
    assert sorted(BUILTIN_FLOOR) == _BUILTINS
    for name, entry in BUILTIN_FLOOR.items():
        mod = importlib.import_module(f"imp.providers.{entry.module}")
        assert callable(mod._env_check), name
        assert callable(mod._factory), name


@pytest.mark.parametrize("name", _MODEL_BEARING)
def test_provider_modules_no_longer_declare_floor_constants(name: str) -> None:
    mod = importlib.import_module(f"imp.providers.{BUILTIN_FLOOR[name].module}")
    assert not hasattr(mod, "TIER_DEFAULTS")
    assert not hasattr(mod, "ENV_VAR")


def test_scripted_keeps_its_runtime_env_var_but_no_floor_table() -> None:
    """scripted reads ENV_VAR at runtime; floor table's env_var is pinned equal."""
    from imp.providers import scripted

    assert not hasattr(scripted, "TIER_DEFAULTS")
    assert scripted.ENV_VAR == "IMP_SCRIPTED_SCRIPT"
    assert BUILTIN_FLOOR["scripted"].env_var == scripted.ENV_VAR


# Exact literals: the hint is user-facing copy, so a plausible-but-wrong string
# must not pass. Table order mirrors the spec's §6 table.
_EXPECTED_AUTH_HINTS = {
    "claude": "run `claude login`",
    "codex": "run `codex login`",
    "anthropic": "set ANTHROPIC_API_KEY",
    "openai": "set OPENAI_API_KEY",
    "scripted": "set IMP_SCRIPTED_SCRIPT",
}


def test_every_builtin_carries_its_auth_hint() -> None:
    assert {n: e.auth_hint for n, e in BUILTIN_FLOOR.items()} == _EXPECTED_AUTH_HINTS


def test_auth_hint_lines_renders_one_indented_line_per_known_provider() -> None:
    from imp.providers import auth_hint_lines

    assert auth_hint_lines(["claude", "anthropic"]) == [
        "  claude — run `claude login`",
        "  anthropic — set ANTHROPIC_API_KEY",
    ]


def test_auth_hint_lines_skips_a_name_outside_the_floor() -> None:
    """A provider registered by a test (or a third party) is not in BUILTIN_FLOOR;
    it must be silently hintless, never a KeyError."""
    from imp.providers import auth_hint_lines

    assert auth_hint_lines(["housecat", "openai"]) == [
        "  openai — set OPENAI_API_KEY",
    ]
    assert auth_hint_lines([]) == []

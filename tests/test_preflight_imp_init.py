"""imp new fails fast with a clean receipt when no provider is available
(never-init'd machine: empty user `providers`) — spec §4."""
from collections.abc import Generator
from pathlib import Path

import pytest

from imp.cli.main import _preflight_imp_init
from imp.providers import register_builtin_providers, resolve


# Generator annotation required: a yielding fixture typed `-> None` fails mypy.
@pytest.fixture(autouse=True)
def _registry() -> Generator[None, None, None]:
    resolve.reset_providers_for_tests()
    register_builtin_providers()
    yield
    resolve.reset_providers_for_tests()
    register_builtin_providers()


def test_preflight_names_the_fix_when_nothing_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The isolated global config sets no `providers:`, so the list is empty —
    sending the user to `imp init` here is a dead end; name the setter instead."""
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert _preflight_imp_init(control_root=tmp_path) == (
        "no providers configured — run 'imp config set providers claude'"
    )


def test_preflight_passes_when_provider_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    imp_home = tmp_path / "imphome"
    imp_home.mkdir(parents=True)
    (imp_home / "config.yaml").write_text("providers: [openai]\n")
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    assert _preflight_imp_init(control_root=tmp_path) is None


def test_preflight_defers_on_unknown_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A providers: entry naming an unregistered provider is a genuine config
    error — preflight DEFERS (returns None) so downstream resolution surfaces
    UnknownProvider with full context, instead of misleading 'run imp init first'."""
    imp_home = tmp_path / "imphome"
    imp_home.mkdir(parents=True)
    (imp_home / "config.yaml").write_text("providers: [does_not_exist]\n")
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    assert _preflight_imp_init(control_root=tmp_path) is None


def _unauthenticated(*names: str) -> None:
    for name in names:
        resolve.register_provider(
            name, env_check=lambda: False, factory=lambda m: None,  # type: ignore[arg-type,return-value]
        )


def test_preflight_lists_each_configured_provider_with_its_auth_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    imp_home = tmp_path / "imphome"
    imp_home.mkdir(parents=True)
    (imp_home / "config.yaml").write_text("providers: [claude, anthropic]\n")
    monkeypatch.setenv("IMP_HOME", str(imp_home))
    _unauthenticated("claude", "anthropic")

    assert _preflight_imp_init(control_root=tmp_path) == (
        "no authenticated provider — configured: claude, anthropic\n"
        "  claude — run `claude login`\n"
        "  anthropic — set ANTHROPIC_API_KEY"
    )


def test_preflight_lists_a_hintless_provider_without_a_hint_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registered provider outside BUILTIN_FLOOR has no hint — it is still
    named on the `configured:` line, and must not raise."""
    imp_home = tmp_path / "imphome"
    imp_home.mkdir(parents=True)
    (imp_home / "config.yaml").write_text("providers: [housecat, openai]\n")
    monkeypatch.setenv("IMP_HOME", str(imp_home))
    _unauthenticated("housecat", "openai")

    assert _preflight_imp_init(control_root=tmp_path) == (
        "no authenticated provider — configured: housecat, openai\n"
        "  openai — set OPENAI_API_KEY"
    )

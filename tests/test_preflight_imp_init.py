"""imp new fails fast with a clean receipt when no provider is available
(never-init'd machine: empty user `providers`) — spec §4."""
from pathlib import Path

import pytest

from imp.cli.main import _preflight_imp_init
from imp.providers import register_builtin_providers, resolve


@pytest.fixture(autouse=True)
def _registry() -> None:
    resolve.reset_providers_for_tests()
    register_builtin_providers()


def test_preflight_blocks_when_no_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    msg = _preflight_imp_init(control_root=tmp_path, project_root=tmp_path)
    assert msg is not None
    assert "imp init" in msg


def test_preflight_passes_when_provider_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg = tmp_path / "xdg" / "imp"
    xdg.mkdir(parents=True)
    (xdg / "config.yaml").write_text("providers: [openai]\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    assert _preflight_imp_init(control_root=tmp_path, project_root=tmp_path) is None


def test_preflight_defers_on_unknown_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A providers: entry naming an unregistered provider is a genuine config
    error — preflight DEFERS (returns None) so downstream resolution surfaces
    UnknownProvider with full context, instead of misleading 'run imp init first'."""
    xdg = tmp_path / "xdg" / "imp"
    xdg.mkdir(parents=True)
    (xdg / "config.yaml").write_text("providers: [does_not_exist]\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert _preflight_imp_init(control_root=tmp_path, project_root=tmp_path) is None

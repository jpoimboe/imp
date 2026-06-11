"""imp config renders the merged config with per-leaf provenance (spec §5)."""
import io
from pathlib import Path

import pytest

from imp.cli.config_cmd import render_config, render_explain
from imp.providers import register_builtin_providers, resolve
from imp.providers.effective import load_effective_config


@pytest.fixture(autouse=True)
def _registry() -> None:
    resolve.reset_providers_for_tests()
    register_builtin_providers()


def _eff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    xdg = tmp_path / "xdg" / "imp"
    xdg.mkdir(parents=True)
    (xdg / "config.yaml").write_text("providers: [openai]\ntheme: light\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return load_effective_config(project_root=None, control_root=None)


def test_render_shows_floor_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eff = _eff(tmp_path, monkeypatch)
    out = io.StringIO()
    render_config(eff, out, color=False)
    text = out.getvalue()
    assert "provider default" in text   # floor leaves attributed to provider code
    assert "high" in text and "opus" in text
    assert "theme" in text and "light" in text  # user-set leaf shown


def test_explain_traces_one_tier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eff = _eff(tmp_path, monkeypatch)
    out = io.StringIO()
    render_explain(eff, "high", out, color=False)
    text = out.getvalue()
    assert "high" in text
    assert "anthropic" in text and "openai" in text


def test_cli_authenticated_provider_skips_key_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """claude_code/codex authenticate via login, not an API key — render must NOT
    fabricate a `CLAUDE_CODE_API_KEY not set` line (finding I1)."""
    xdg = tmp_path / "xdg" / "imp"
    xdg.mkdir(parents=True)
    (xdg / "config.yaml").write_text("providers: [claude_code]\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    eff = load_effective_config(project_root=None, control_root=None)
    out = io.StringIO()
    render_config(eff, out, color=False)
    text = out.getvalue()
    assert "(CLI-authenticated)" in text
    assert "CLAUDE_CODE_API_KEY" not in text


def test_render_lists_available_drivers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eff = _eff(tmp_path, monkeypatch)            # no remote.driver configured
    out = io.StringIO()
    render_config(eff, out, color=False)
    text = out.getvalue()
    assert "available drivers:" in text
    assert "telegram" in text
    assert "IMP_TELEGRAM_TOKEN" in text and "IMP_TELEGRAM_CHAT_ID" in text
    assert "(available)" in text                 # per-driver state marker, not enabled
    assert "enabled" not in text


def test_render_marks_enabled_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg = tmp_path / "xdg" / "imp"
    xdg.mkdir(parents=True)
    (xdg / "config.yaml").write_text("providers: [openai]\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    (tmp_path / ".imp").mkdir()
    (tmp_path / ".imp" / "config.yaml").write_text("remote:\n  driver: telegram\n")
    eff = load_effective_config(project_root=None, control_root=tmp_path)
    out = io.StringIO()
    render_config(eff, out, color=False)
    assert "telegram" in out.getvalue()
    assert "(enabled)" in out.getvalue()         # the configured driver's state marker

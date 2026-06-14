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
    render_config(eff, out, color=False, show_all=True)
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


def test_config_default_lists_only_configured_provider_tiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eff = _eff(tmp_path, monkeypatch)            # providers: [openai]
    out = io.StringIO()
    render_config(eff, out, color=False)         # default: show_all=False
    text = out.getvalue()
    assert "openai" in text
    assert "anthropic" not in text               # non-configured vendor hidden


def test_config_all_lists_full_provider_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eff = _eff(tmp_path, monkeypatch)
    out = io.StringIO()
    render_config(eff, out, color=False, show_all=True)
    text = out.getvalue()
    assert "openai" in text and "anthropic" in text


def test_config_default_shows_full_matrix_when_no_providers_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # When no providers: key is set, cfg.providers is (), the "and configured"
    # guard short-circuits to False, and the full matrix is shown even with
    # show_all=False.
    xdg = tmp_path / "xdg" / "imp"
    xdg.mkdir(parents=True)
    (xdg / "config.yaml").write_text("theme: ansi\n")  # no providers: key
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    eff = load_effective_config(project_root=None, control_root=None)
    out = io.StringIO()
    render_config(eff, out, color=False)              # show_all=False (default)
    text = out.getvalue()
    assert "anthropic" in text
    assert "openai" in text


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


def test_explain_colors_sources_when_layers_differ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg = tmp_path / "xdg" / "imp"
    xdg.mkdir(parents=True)
    # User overrides only the openai binding of `high`; anthropic stays at the
    # provider-default floor — so the two rows' sources differ.
    (xdg / "config.yaml").write_text(
        "providers: [anthropic, openai]\n"
        "tiers:\n"
        "  high:\n"
        "    openai: gpt-5-custom\n"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    eff = load_effective_config(project_root=None, control_root=None)
    out = io.StringIO()
    render_explain(eff, "high", out, color=True)
    assert "\x1b[" in out.getvalue()   # labels always colored when color=True
    output = out.getvalue()
    anthropic_line = next(ln for ln in output.splitlines() if "anthropic" in ln)
    openai_line = next(ln for ln in output.splitlines() if "openai" in ln)
    assert "\x1b[2m" in anthropic_line   # provenance always dim
    assert "\x1b[2m" in openai_line     # provenance always dim


def test_explain_colors_even_when_layers_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eff = _eff(tmp_path, monkeypatch)   # no tier overrides → all floor (default layer)
    out = io.StringIO()
    render_explain(eff, "high", out, color=True)
    output = out.getvalue()
    assert "\x1b[2m←" in output         # arrow + label colored dim, even all-default


def test_explain_never_colors_under_no_color(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg = tmp_path / "xdg" / "imp"
    xdg.mkdir(parents=True)
    (xdg / "config.yaml").write_text(
        "providers: [anthropic, openai]\n"
        "tiers:\n"
        "  high:\n"
        "    openai: gpt-5-custom\n"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    eff = load_effective_config(project_root=None, control_root=None)
    out = io.StringIO()
    render_explain(eff, "high", out, color=False)
    assert "\x1b[" not in out.getvalue()


def test_explain_bolds_value_and_dims_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eff = _eff(tmp_path, monkeypatch)
    out = io.StringIO()
    render_explain(eff, "high", out, color=True)
    text = out.getvalue()
    # The resolved value is emphasized (bold SGR \x1b[1m) and the provenance
    assert "\x1b[1m" in text   # bold value
    assert "\x1b[2m" in text   # dim provenance
    assert "←" in text


def test_explain_plain_unchanged_under_no_color(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eff = _eff(tmp_path, monkeypatch)
    out = io.StringIO()
    render_explain(eff, "high", out, color=False)
    text = out.getvalue()
    assert "\x1b[" not in text          # no SGR escapes at all
    assert "anthropic" in text and "←" in text

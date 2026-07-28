"""imp config renders the merged config with per-leaf provenance (spec §5)."""
import io
from pathlib import Path

import pytest

from imp.cli.config_cmd import layer_label, render_config, render_explain
from imp.providers import register_builtin_providers, resolve
from imp.providers.effective import FLOOR_SRC, global_config_path, load_effective_config


@pytest.fixture(autouse=True)
def _registry() -> None:
    resolve.reset_providers_for_tests()
    register_builtin_providers()


def _eff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    imp_home = tmp_path / "imphome"
    imp_home.mkdir(parents=True)
    (imp_home / "config.yaml").write_text("providers: [openai]\ntheme: light\n")
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    return load_effective_config()


def _block(lines: list[str], header: str) -> list[str]:
    """The rows of one `imp config` section, up to its blank separator line."""
    rows: list[str] = []
    for ln in lines[lines.index(header) + 1:]:
        if ln == "":
            break
        rows.append(ln)
    return rows


def test_render_shows_floor_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eff = _eff(tmp_path, monkeypatch)
    out = io.StringIO()
    render_config(eff, out, color=False, show_all=True)
    text = out.getvalue()
    assert "provider default" in text   # floor leaves attributed to provider code
    assert "high" in text and "opus" in text
    assert "theme" in text and "light" in text  # user-set leaf shown


def test_render_shows_test_cmd_unset_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eff = _eff(tmp_path, monkeypatch)
    out = io.StringIO()
    render_config(eff, out, color=False)
    text = out.getvalue()
    lines = text.splitlines()
    fast_line = next((ln.strip() for ln in lines if ln.strip().startswith("test_cmd_fast ")), None)
    slow_line = next((ln.strip() for ln in lines if ln.strip().startswith("test_cmd_slow ")), None)
    assert fast_line is not None and "(unset)" in fast_line
    assert slow_line is not None and "(unset)" in slow_line
    assert "test_cmd_fast_timeout" in text and "(default 120s)" in text
    assert "test_cmd_slow_timeout" in text and "(default 300s)" in text
    assert "verify_command" not in text and "verify_timeout" not in text


def test_render_shows_configured_test_cmds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    imp_home = tmp_path / "imphome"
    imp_home.mkdir(parents=True)
    (imp_home / "config.yaml").write_text(
        "providers: [openai]\n"
        "test_cmd_fast: make check\n"
        "test_cmd_slow: make check && make test-live\n"
        "test_cmd_slow_timeout: 600\n"
    )
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    eff = load_effective_config()
    out = io.StringIO()
    render_config(eff, out, color=False)
    text = out.getvalue()
    lines = text.splitlines()
    fast_line = next((ln.strip() for ln in lines if ln.strip().startswith("test_cmd_fast ")), None)
    slow_line = next((ln.strip() for ln in lines if ln.strip().startswith("test_cmd_slow ")), None)
    assert fast_line is not None and "make check" in fast_line
    assert "make check && make test-live" not in fast_line
    assert slow_line is not None and "make check && make test-live" in slow_line
    assert "600s" in text


def test_render_shows_disabled_test_cmds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    imp_home = tmp_path / "imphome"
    imp_home.mkdir(parents=True)
    (imp_home / "config.yaml").write_text(
        "providers: [openai]\n"
        'test_cmd_fast: ""\n'
        'test_cmd_slow: ""\n'
    )
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    eff = load_effective_config()
    out = io.StringIO()
    render_config(eff, out, color=False)
    text = out.getvalue()
    lines = text.splitlines()
    fast_line = next((ln.strip() for ln in lines if ln.strip().startswith("test_cmd_fast ")), None)
    slow_line = next((ln.strip() for ln in lines if ln.strip().startswith("test_cmd_slow ")), None)
    assert fast_line is not None and "(disabled)" in fast_line
    assert slow_line is not None and "(disabled)" in slow_line


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
    imp_home = tmp_path / "imphome"
    imp_home.mkdir(parents=True)
    (imp_home / "config.yaml").write_text("providers: [claude_code]\n")
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    eff = load_effective_config()
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
    imp_home = tmp_path / "imphome"
    imp_home.mkdir(parents=True)
    (imp_home / "config.yaml").write_text("theme: ansi\n")  # no providers: key
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    eff = load_effective_config()
    out = io.StringIO()
    render_config(eff, out, color=False)              # show_all=False (default)
    text = out.getvalue()
    assert "anthropic" in text
    assert "openai" in text


def test_render_marks_enabled_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    imp_home = tmp_path / "imphome"
    imp_home.mkdir(parents=True)
    (imp_home / "config.yaml").write_text("providers: [openai]\n")
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    (tmp_path / ".imp").mkdir()
    (tmp_path / ".imp" / "config.yaml").write_text("remote:\n  driver: telegram\n")
    eff = load_effective_config(control_root=tmp_path)
    out = io.StringIO()
    render_config(eff, out, color=False)
    assert "telegram" in out.getvalue()
    assert "(enabled)" in out.getvalue()         # the configured driver's state marker


def test_explain_colors_sources_when_layers_differ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    imp_home = tmp_path / "imphome"
    imp_home.mkdir(parents=True)
    # User overrides only the openai binding of `high`; anthropic stays at the
    # provider-default floor — so the two rows' sources differ.
    (imp_home / "config.yaml").write_text(
        "providers: [anthropic, openai]\n"
        "tiers:\n"
        "  high:\n"
        "    openai: gpt-5-custom\n"
    )
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    eff = load_effective_config()
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
    assert "\x1b[2m[" in output


def test_explain_never_colors_under_no_color(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    imp_home = tmp_path / "imphome"
    imp_home.mkdir(parents=True)
    (imp_home / "config.yaml").write_text(
        "providers: [anthropic, openai]\n"
        "tiers:\n"
        "  high:\n"
        "    openai: gpt-5-custom\n"
    )
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    eff = load_effective_config()
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
    assert "[provider default]" in text


def test_explain_plain_unchanged_under_no_color(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eff = _eff(tmp_path, monkeypatch)
    out = io.StringIO()
    render_explain(eff, "high", out, color=False)
    text = out.getvalue()
    assert "\x1b[" not in text          # no SGR escapes at all
    assert "anthropic" in text and "[provider default]" in text
    assert "←" not in text


def test_render_shows_configured_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    imp_home = tmp_path / "imphome"
    imp_home.mkdir(parents=True)
    (imp_home / "config.yaml").write_text("providers: [openai]\npythonpath: [src, lib]\n")
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    eff = load_effective_config()
    out = io.StringIO()
    render_config(eff, out, color=False)
    line = next((ln.strip() for ln in out.getvalue().splitlines()
                 if ln.strip().startswith("pythonpath ")), None)
    assert line is not None and "src, lib" in line


def test_config_brackets_align_across_tiers_and_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    imp_home = tmp_path / "imphome"
    imp_home.mkdir(parents=True)
    (imp_home / "config.yaml").write_text(
        "providers: [openai]\n"
        "test_cmd_slow: make check && make test-live\n"   # 28 chars, exceeds 24
    )
    monkeypatch.setenv("IMP_HOME", str(imp_home))
    eff = load_effective_config()
    out = io.StringIO()
    render_config(eff, out, color=False)
    lines = out.getvalue().splitlines()
    tiers = _block(lines, "tiers:")
    settings = _block(lines, "settings:")
    assert len(tiers) >= 2 and len(settings) >= 2
    # One shared value width: the tiers block's short model names must not pull
    # its brackets left of the settings block's long test command.
    assert len({ln.rindex("[") for ln in [*tiers, *settings]}) == 1
    assert "make check && make test-live" in out.getvalue()


def test_render_shows_unset_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eff = _eff(tmp_path, monkeypatch)  # config with no pythonpath
    out = io.StringIO()
    render_config(eff, out, color=False)
    line = next((ln.strip() for ln in out.getvalue().splitlines()
                 if ln.strip().startswith("pythonpath ")), None)
    assert line is not None and "(unset)" in line


def test_layer_label_names_each_layer_in_its_short_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "elsewhere"))
    assert layer_label(FLOOR_SRC.file) == "provider default"
    assert layer_label(tmp_path / "repo" / "imp" / "config.yaml") == "imp/config.yaml"
    assert layer_label(tmp_path / "repo" / ".imp" / "config.yaml") == ".imp/config.yaml"


def test_layer_label_renders_the_global_path_home_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # HOME is patched only to exercise the ~-relative rule on a pure string
    # helper — no git, no credential lookup, and IMP_HOME still isolates the
    # live global layer.
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("IMP_HOME", str(home / ".imp"))
    assert layer_label(global_config_path()) == "~/.imp/config.yaml"


def test_layer_label_renders_the_global_path_absolute_outside_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "elsewhere"))
    assert layer_label(global_config_path()) == str(tmp_path / "elsewhere" / "config.yaml")


def test_config_never_elides_a_provenance_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eff = _eff(tmp_path, monkeypatch)   # global config at <IMP_HOME>/config.yaml
    out = io.StringIO()
    render_config(eff, out, color=False, show_all=True)
    text = out.getvalue()
    assert "…/" not in text                     # the two-segment elision is gone
    theme_line = next(ln for ln in text.splitlines()
                      if ln.strip().startswith("theme "))
    assert theme_line.rstrip().endswith(f"[{global_config_path()}]")


def test_settings_block_leads_with_default_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    imp_home = tmp_path / "imphome"
    imp_home.mkdir(parents=True)
    (imp_home / "config.yaml").write_text("providers: [openai]\n")
    monkeypatch.setenv("IMP_HOME", str(imp_home))
    proj = tmp_path / "proj"
    (proj / "imp").mkdir(parents=True)
    (proj / "imp" / "config.yaml").write_text("default-workflow: mine\n")
    eff = load_effective_config(control_root=proj)
    out = io.StringIO()
    render_config(eff, out, color=False)
    settings = _block(out.getvalue().splitlines(), "settings:")
    assert settings[0].split()[0] == "default-workflow"
    assert "mine" in settings[0]
    assert settings[0].rstrip().endswith("[imp/config.yaml]")


def test_config_dims_tiers_block_provenance_when_color(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eff = _eff(tmp_path, monkeypatch)   # providers: [openai]
    out = io.StringIO()
    render_config(eff, out, color=True, show_all=True)
    tier_line = next(ln for ln in out.getvalue().splitlines()
                     if ln.lstrip().startswith("high openai"))
    assert "\x1b[2m[" in tier_line
    assert tier_line.rstrip().endswith("\x1b[0m")   # dim closed with a reset


def test_config_dims_settings_block_provenance_when_color(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eff = _eff(tmp_path, monkeypatch)   # theme: light is a user-set leaf
    out = io.StringIO()
    render_config(eff, out, color=True)
    theme_line = next(ln for ln in out.getvalue().splitlines()
                      if ln.lstrip().startswith("theme "))
    assert "\x1b[2m[" in theme_line


def test_config_leaves_marks_and_values_plain_when_color(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eff = _eff(tmp_path, monkeypatch)
    out = io.StringIO()
    render_config(eff, out, color=True)
    lines = out.getvalue().splitlines()
    mark_line = next(ln for ln in lines if "✓" in ln or "✗" in ln)
    assert "\x1b[" not in mark_line      # provider-key marks carry no SGR
    theme_line = next(ln for ln in lines if ln.lstrip().startswith("theme "))
    value_part = theme_line.split("\x1b[2m")[0]   # everything left of the dim annotation
    assert "\x1b[" not in value_part     # key + value stay plain


def test_config_no_color_is_byte_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eff = _eff(tmp_path, monkeypatch)
    out = io.StringIO()
    render_config(eff, out, color=False, show_all=True)
    assert "\x1b[" not in out.getvalue()   # piped/NO_COLOR output stays byte-clean
    assert "←" not in out.getvalue()

"""imp config renders the merged config with per-leaf provenance (spec §5)."""
import io
from pathlib import Path

import pytest

from imp.cli.config_cmd import layer_label, render_config, render_explain
from imp.paths import display_path, project_state_dir
from imp.providers import register_builtin_providers, resolve
from imp.providers.effective import FLOOR_SRC, load_effective_config
from imp.providers.paths import global_config_path
from tests._readiness import pin_readiness


@pytest.fixture(autouse=True)
def _registry() -> None:
    resolve.reset_providers_for_tests()
    register_builtin_providers()


@pytest.fixture(autouse=True)
def _pinned_readiness(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory,
) -> None:
    pin_readiness(monkeypatch, tmp_path_factory.mktemp("readiness"))


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
    render_config(eff, out, state_dir=None, color=False, show_all=True)
    text = out.getvalue()
    assert "provider default" in text   # floor leaves attributed to provider code
    assert "high" in text and "opus" in text
    assert "theme" in text and "light" in text  # user-set leaf shown


def test_render_shows_sol_as_codex_high_provider_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    imp_home = tmp_path / "imphome"
    imp_home.mkdir()
    (imp_home / "config.yaml").write_text("providers: [codex]\n")
    monkeypatch.setenv("IMP_HOME", str(imp_home))
    eff = load_effective_config()
    out = io.StringIO()

    render_config(eff, out, state_dir=None, color=False)

    row = next(
        line.strip()
        for line in out.getvalue().splitlines()
        if line.strip().startswith("high codex ")
    )
    assert row.split()[:3] == ["high", "codex", "gpt-6.1-sol"]
    assert row.endswith("[provider default]")


def test_render_shows_test_cmd_unset_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eff = _eff(tmp_path, monkeypatch)
    out = io.StringIO()
    render_config(eff, out, state_dir=None, color=False)
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
    render_config(eff, out, state_dir=None, color=False)
    text = out.getvalue()
    lines = text.splitlines()
    fast_line = next((ln.strip() for ln in lines if ln.strip().startswith("test_cmd_fast ")), None)
    slow_line = next((ln.strip() for ln in lines if ln.strip().startswith("test_cmd_slow ")), None)
    assert fast_line is not None and "make check" in fast_line
    assert "make check && make test-live" not in fast_line
    assert slow_line is not None and "make check && make test-live" in slow_line
    assert "600s" in text


def test_render_shows_none_needed_for_a_decided_false_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    imp_home = tmp_path / "imphome"
    imp_home.mkdir(parents=True)
    (imp_home / "config.yaml").write_text(
        "providers: [openai]\n"
        "test_cmd_fast: false\n"
        "test_cmd_slow: false\n"
        "worktree_setup: false\n"
        "pythonpath: false\n"
    )
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    eff = load_effective_config()
    out = io.StringIO()
    render_config(eff, out, state_dir=None, color=False)
    text = out.getvalue()
    lines = text.splitlines()
    for key in ("test_cmd_fast", "test_cmd_slow", "worktree_setup", "pythonpath"):
        line = next((ln.strip() for ln in lines
                     if ln.strip().startswith(f"{key} ")), None)
        assert line is not None and "(none needed)" in line, key
    assert "(disabled)" not in text


def test_render_shows_a_decided_pythonpath_as_its_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    imp_home = tmp_path / "imphome"
    imp_home.mkdir(parents=True)
    (imp_home / "config.yaml").write_text(
        "providers: [openai]\npythonpath: [src, vendor]\n")
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    out = io.StringIO()
    render_config(load_effective_config(), out, state_dir=None, color=False)
    line = next((ln.strip() for ln in out.getvalue().splitlines()
                 if ln.strip().startswith("pythonpath ")), None)
    assert line is not None and "src, vendor" in line


def test_explain_traces_one_tier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eff = _eff(tmp_path, monkeypatch)
    out = io.StringIO()
    render_explain(eff, "high", out, color=False)
    text = out.getvalue()
    assert "high" in text
    assert "anthropic" in text and "openai" in text


def test_a_signed_out_claude_row_reads_not_signed_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """claude/codex authenticate via login, not an API key — the row must NOT
    fabricate a `CLAUDE_API_KEY not set` verdict (finding I1)."""
    pin_readiness(monkeypatch, tmp_path_factory.mktemp("signed-out"), keys=())
    imp_home = tmp_path / "imphome"
    imp_home.mkdir(parents=True)
    (imp_home / "config.yaml").write_text("providers: [claude]\n")
    monkeypatch.setenv("IMP_HOME", str(imp_home))
    out = io.StringIO()
    render_config(load_effective_config(), out, state_dir=None, color=False)
    text = out.getvalue()
    assert _block(text.splitlines(), "providers (local check):") == [
        "  ✗ claude         not signed in",
    ]
    assert "CLAUDE_API_KEY" not in text
    assert "(CLI-authenticated)" not in text


def test_render_lists_available_drivers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eff = _eff(tmp_path, monkeypatch)            # no remote.driver configured
    out = io.StringIO()
    render_config(eff, out, state_dir=None, color=False)
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
    render_config(eff, out, state_dir=None, color=False)         # default: show_all=False
    text = out.getvalue()
    assert "openai" in text
    assert "anthropic" not in text               # non-configured vendor hidden


def test_config_all_lists_full_provider_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eff = _eff(tmp_path, monkeypatch)
    out = io.StringIO()
    render_config(eff, out, state_dir=None, color=False, show_all=True)
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
    render_config(eff, out, state_dir=None, color=False)              # show_all=False (default)
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
    (project_state_dir(tmp_path) / "config.yaml").write_text("remote:\n  driver: telegram\n")
    eff = load_effective_config(control_root=tmp_path)
    out = io.StringIO()
    render_config(eff, out, state_dir=None, color=False)
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
    render_config(eff, out, state_dir=None, color=False)
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
    render_config(eff, out, state_dir=None, color=False)
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
    render_config(eff, out, state_dir=None, color=False)
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
    render_config(eff, out, state_dir=None, color=False, show_all=True)
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
    render_config(eff, out, state_dir=None, color=False)
    settings = _block(out.getvalue().splitlines(), "settings:")
    assert settings[0].split()[0] == "default-workflow"
    assert "mine" in settings[0]
    assert settings[0].rstrip().endswith("[imp/config.yaml]")


def test_config_dims_tiers_block_provenance_when_color(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eff = _eff(tmp_path, monkeypatch)   # providers: [openai]
    out = io.StringIO()
    render_config(eff, out, state_dir=None, color=True, show_all=True)
    tier_line = next(ln for ln in out.getvalue().splitlines()
                     if ln.lstrip().startswith("high openai"))
    assert "\x1b[2m[" in tier_line
    assert tier_line.rstrip().endswith("\x1b[0m")   # dim closed with a reset


def test_layer_label_renders_the_four_layers_distinctly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under a default $IMP_HOME the local layer must not collapse onto the
    project layer's `<parent>/<name>` form (spec AC 40a)."""
    home = tmp_path / "home"
    repo = home / "repo"
    repo.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("IMP_HOME", raising=False)

    labels = [
        layer_label(FLOOR_SRC.file),
        layer_label(repo / "imp" / "config.yaml"),
        layer_label(global_config_path()),
        layer_label(project_state_dir(repo) / "config.yaml"),
    ]

    assert labels == [
        "provider default",
        "imp/config.yaml",
        "~/.imp/config.yaml",
        "~/.imp/projects/repo/config.yaml",
    ]
    assert len(set(labels)) == 4


def test_config_dims_settings_block_provenance_when_color(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eff = _eff(tmp_path, monkeypatch)   # theme: light is a user-set leaf
    out = io.StringIO()
    render_config(eff, out, state_dir=None, color=True)
    theme_line = next(ln for ln in out.getvalue().splitlines()
                      if ln.lstrip().startswith("theme "))
    assert "\x1b[2m[" in theme_line


def test_config_leaves_marks_and_values_plain_when_color(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eff = _eff(tmp_path, monkeypatch)
    out = io.StringIO()
    render_config(eff, out, state_dir=None, color=True)
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
    render_config(eff, out, state_dir=None, color=False, show_all=True)
    assert "\x1b[" not in out.getvalue()   # piped/NO_COLOR output stays byte-clean
    assert "←" not in out.getvalue()


def test_config_leads_with_the_project_state_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eff = _eff(tmp_path, monkeypatch)
    state = tmp_path / "imphome" / "projects" / "repo"
    out = io.StringIO()

    render_config(eff, out, state_dir=state, color=False)

    lines = out.getvalue().splitlines()
    assert lines[0] == f"project state: {display_path(state)}"
    assert lines[1] == ""
    assert lines[2] == "tiers:"


def test_config_marks_an_unclaimed_state_dir_not_created_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eff = _eff(tmp_path, monkeypatch)
    state = tmp_path / "imphome" / "projects" / "repo"
    out = io.StringIO()

    render_config(eff, out, state_dir=state, state_claimed=False, color=False)

    lines = out.getvalue().splitlines()
    assert lines[0] == f"project state: {display_path(state)} (not created yet)"
    assert lines[1] == ""
    assert not state.exists()


def test_config_omits_the_state_line_outside_a_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eff = _eff(tmp_path, monkeypatch)
    out = io.StringIO()

    render_config(eff, out, state_dir=None, color=False)

    assert out.getvalue().splitlines()[0] == "tiers:"
    assert "project state:" not in out.getvalue()


def test_render_shows_unset_worktree_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eff = _eff(tmp_path, monkeypatch)  # no layer sets worktree_setup
    out = io.StringIO()
    render_config(eff, out, state_dir=None, color=False)
    settings = _block(out.getvalue().splitlines(), "settings:")
    line = next((ln.strip() for ln in settings
                 if ln.strip().startswith("worktree_setup ")), None)
    assert line is not None and "(unset)" in line
    assert line.rstrip().endswith("[default]")


def test_render_shows_configured_worktree_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    imp_home = tmp_path / "imphome"
    imp_home.mkdir(parents=True)
    (imp_home / "config.yaml").write_text("providers: [openai]\n")
    monkeypatch.setenv("IMP_HOME", str(imp_home))
    proj = tmp_path / "proj"
    (proj / "imp").mkdir(parents=True)
    (proj / "imp" / "config.yaml").write_text(
        "worktree_setup: uv sync --all-extras\n")
    eff = load_effective_config(control_root=proj)
    out = io.StringIO()
    render_config(eff, out, state_dir=None, color=False)
    settings = _block(out.getvalue().splitlines(), "settings:")
    line = next((ln.strip() for ln in settings
                 if ln.strip().startswith("worktree_setup ")), None)
    assert line is not None and "uv sync --all-extras" in line
    assert line.rstrip().endswith("[imp/config.yaml]")


def test_provider_rows_carry_a_mark_and_the_shared_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    pin_readiness(monkeypatch, tmp_path_factory.mktemp("rows"),
                  on_path=("claude",), keys=("ANTHROPIC_API_KEY",))
    imp_home = tmp_path / "imphome"
    imp_home.mkdir(parents=True)
    (imp_home / "config.yaml").write_text(
        "providers: [claude, anthropic, openai, codex]\n"
        "provider_envs: {codex: {env: MY_CODEX_KEY}}\n")
    monkeypatch.setenv("IMP_HOME", str(imp_home))
    monkeypatch.delenv("MY_CODEX_KEY", raising=False)
    out = io.StringIO()
    render_config(load_effective_config(), out, state_dir=None, color=False)
    assert _block(out.getvalue().splitlines(), "providers (local check):") == [
        "  ✓ claude         signed in",
        "  ✓ anthropic      ANTHROPIC_API_KEY set",
        "  ✗ openai         OPENAI_API_KEY not set",
        "  ✗ codex          MY_CODEX_KEY not set",
    ]


def test_a_missing_cli_row_reads_cli_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    pin_readiness(monkeypatch, tmp_path_factory.mktemp("no-cli"), on_path=())
    imp_home = tmp_path / "imphome"
    imp_home.mkdir(parents=True)
    (imp_home / "config.yaml").write_text("providers: [codex]\n")
    monkeypatch.setenv("IMP_HOME", str(imp_home))
    out = io.StringIO()
    render_config(load_effective_config(), out, state_dir=None, color=False)
    assert _block(out.getvalue().splitlines(), "providers (local check):") == [
        "  ✗ codex          CLI not found",
    ]


@pytest.mark.parametrize("show_all", [True, False])
def test_config_lists_tiers_high_medium_low(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, show_all: bool
) -> None:
    eff = _eff(tmp_path, monkeypatch)
    out = io.StringIO()
    render_config(eff, out, state_dir=None, color=False, show_all=show_all)
    rows = [ln.split()[:2] for ln in _block(out.getvalue().splitlines(), "tiers:")]
    tiers = [tier for tier, _ in rows]
    assert list(dict.fromkeys(tiers)) == ["high", "medium", "low"]
    assert tiers == sorted(tiers, key=["high", "medium", "low"].index)
    for tier in ("high", "medium", "low"):
        vendors = [v for t, v in rows if t == tier]
        assert vendors == sorted(vendors)
        if show_all:
            assert len(vendors) > 1

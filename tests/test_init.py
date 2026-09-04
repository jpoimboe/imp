"""`imp init` scaffolding: split committed imp/config.yaml + bootstrapped
global config (spec §4)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
import yaml

from imp.cli.init import run_init
from tests.conftest import _git, _init_repo


def _inputs(*answers: str) -> object:
    it = iter(answers)
    return lambda _prompt="": next(it)


def _picks(*selected: str) -> object:
    return lambda _available: list(selected)


def _stub_screen(monkeypatch, **overrides) -> None:
    """Accept the detected state on the confirmation screen, any row overridden."""
    from dataclasses import replace

    import imp.cli.init as initmod

    def _screen(_root, state, **_kw):
        return initmod.ScreenAccepted(replace(state, **overrides))

    monkeypatch.setattr(initmod, "_interactive_confirm_screen", _screen,
                        raising=False)


def _stub_screen_recording_theme(monkeypatch, seen: dict) -> None:
    """Stub the confirmation screen and record the palette theme active while it runs."""
    import imp.cli.init as initmod
    from imp.cli import palette as pal

    def _screen(_root, state, **_kw):
        seen["theme"] = pal.active_theme()
        return initmod.ScreenAccepted(state)

    monkeypatch.setattr(initmod, "_interactive_confirm_screen", _screen,
                        raising=False)


@pytest.fixture(autouse=True)
def _isolate_global_config(monkeypatch, tmp_path):
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    # This override shadows conftest's autouse fixture for the module, so it must
    # neuter the migration's legacy source too.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".xdg-isolated"))


@pytest.fixture(autouse=True)
def _stub_theme_detection(monkeypatch):
    monkeypatch.setattr("imp.cli.init._default_detect_theme", lambda: "ansi")


@pytest.fixture(autouse=True)
def _git_repo(tmp_path, monkeypatch):
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")


@pytest.fixture(autouse=True)
def _stub_setup_gate(monkeypatch):
    # Fresh interactive init runs the sandbox gate, then the confirmation screen.
    # Default them to "ready + accept as detected"; specific tests override.
    import imp.cli.init as initmod
    monkeypatch.setattr(initmod, "sandbox_available", lambda: True, raising=False)
    _stub_screen(monkeypatch)


def test_writes_config_yaml_workflows_gitignore(tmp_path: Path) -> None:
    run_init(
        project_root=tmp_path,
        picker_fn=_picks("anthropic"),
        input_fn=_inputs(),
        output_fn=lambda _msg: None,
    )
    proj = tmp_path / "imp" / "config.yaml"
    assert proj.is_file()
    data = yaml.safe_load(proj.read_text())
    # The committed project file carries the workflow ref + project settings —
    # NO bindings (the floor owns tiers), NO providers/theme (global-homed).
    assert data["default-workflow"] == "default"
    assert "workflow" not in data
    assert "tiers" not in data
    assert "providers" not in data
    assert "provider_envs" not in data
    assert "theme" not in data
    assert "roles" not in data

    assert not (tmp_path / "imp" / "workflows").exists()
    # No per-.imp .gitignore anymore; .imp/ is ignored wholesale.
    assert not (tmp_path / ".imp").exists()


def test_bootstraps_user_config_with_providers(tmp_path: Path) -> None:
    run_init(
        project_root=tmp_path, picker_fn=_picks("anthropic"),
        input_fn=_inputs(), output_fn=lambda _msg: None,
    )
    user = tmp_path / "imphome" / "config.yaml"
    assert user.is_file()
    data = yaml.safe_load(user.read_text())
    assert data["providers"] == ["anthropic"]
    assert data["editing_mode"] == "emacs"


def test_persists_detected_theme(tmp_path: Path) -> None:
    # The detected theme is written to the global config so the runtime never
    # re-queries the terminal.
    run_init(
        project_root=tmp_path, picker_fn=_picks("anthropic"),
        input_fn=_inputs(), output_fn=lambda _msg: None,
        detect_theme_fn=lambda: "dark",
    )
    data = yaml.safe_load((tmp_path / "imphome" / "config.yaml").read_text())
    assert data["theme"] == "dark"


def test_user_config_documents_theme_and_editing_mode(tmp_path: Path) -> None:
    # safe_dump can't emit inline comments, so these are appended as raw commented lines.
    run_init(
        project_root=tmp_path, picker_fn=_picks("anthropic"),
        input_fn=_inputs(), output_fn=lambda _msg: None,
        detect_theme_fn=lambda: "dark",
    )
    text = (tmp_path / "imphome" / "config.yaml").read_text()
    assert "theme: dark  # dark | light | ansi\n" in text
    assert "editing_mode: emacs  # emacs | vi\n" in text
    assert text.index("providers:") < text.index("theme:") < text.index("editing_mode:")


def test_appends_to_project_gitignore(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("# existing\nfoo/\n")
    run_init(
        project_root=tmp_path, picker_fn=_picks("anthropic"),
        input_fn=_inputs(),
        output_fn=lambda _msg: None,
    )
    text = (tmp_path / ".gitignore").read_text()
    assert "foo/" in text and ".imp/" in text


def test_init_idempotent_on_existing_clone(tmp_path: Path) -> None:
    # A clone that already carries the committed project file must NOT raise —
    # init just bootstraps the machine's global config.
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text("default-workflow: imp\n")
    run_init(
        project_root=tmp_path, picker_fn=_picks("anthropic"),
        input_fn=_inputs(),
        output_fn=lambda _msg: None,
    )
    # Project file untouched; global config bootstrapped.
    assert (tmp_path / "imp" / "config.yaml").read_text() == "default-workflow: imp\n"
    assert (tmp_path / "imphome" / "config.yaml").exists()


def test_supports_two_providers_recorded_in_user_config(tmp_path: Path) -> None:
    run_init(
        project_root=tmp_path,
        picker_fn=_picks("claude_code", "openai"),
        input_fn=_inputs(),
        output_fn=lambda _msg: None,
    )
    data = yaml.safe_load((tmp_path / "imphome" / "config.yaml").read_text())
    assert data["providers"] == ["claude_code", "openai"]
    # Bindings are NOT scaffolded — the provider-code floor owns tiers.
    proj = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    assert "tiers" not in proj


def test_supports_claude_code_no_env_prompt(tmp_path: Path) -> None:
    def _no_input(_prompt: str = "") -> str:
        raise AssertionError(f"unexpected input prompt: {_prompt!r}")

    run_init(
        project_root=tmp_path, picker_fn=_picks("claude_code"),
        input_fn=_no_input, output_fn=lambda _msg: None,
    )
    data = yaml.safe_load((tmp_path / "imphome" / "config.yaml").read_text())
    assert data["providers"] == ["claude_code"]


def test_picker_sees_all_providers(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def _picker(available: list[str]) -> list[str]:
        seen.append(list(available))
        return ["anthropic"]

    run_init(
        project_root=tmp_path, picker_fn=_picker,
        input_fn=_inputs(),
        output_fn=lambda _msg: None,
    )
    assert seen == [["claude_code", "anthropic", "openai", "codex"]]


def test_empty_picker_result_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one provider"):
        run_init(
            project_root=tmp_path, picker_fn=lambda _available: [],
            input_fn=_inputs(), output_fn=lambda _msg: None,
        )


def test_init_writes_test_cmd_fast_from_the_screen(tmp_path, monkeypatch):
    _stub_screen(monkeypatch, test_cmd_fast="make check")
    run_init(tmp_path, picker_fn=_picks("anthropic"), input_fn=_inputs(),
             output_fn=lambda _m: None)
    data = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    assert data["test_cmd_fast"] == "make check"


def test_init_writes_needs_none_when_the_screen_opts_out(tmp_path, monkeypatch):
    _stub_screen(monkeypatch, test_cmd_fast=False, tests_provenance="you set this")
    run_init(tmp_path, picker_fn=_picks("anthropic"), input_fn=_inputs(),
             output_fn=lambda _m: None)
    data = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    assert data["test_cmd_fast"] is False


def test_init_comment_names_timeout_defaults(tmp_path, monkeypatch):
    _stub_screen(monkeypatch, test_cmd_fast="make check")
    run_init(tmp_path, picker_fn=_picks("anthropic"), input_fn=_inputs(),
             output_fn=lambda _m: None)
    text = (tmp_path / "imp" / "config.yaml").read_text()
    assert "120s" in text and "300s" in text
    assert "test_cmd_fast_timeout" in text
    assert "test_cmd_slow" in text and "test_cmd_slow_timeout" in text
    assert "Empty string opts out" not in text
    assert "false declares the project needs none" in text


def test_init_available_bwrap_is_quiet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import imp.cli.init as initmod
    monkeypatch.setattr(initmod, "sandbox_available", lambda: True, raising=False)
    msgs: list[str] = []
    run_init(
        project_root=tmp_path,
        picker_fn=_picks("anthropic"),
        input_fn=_inputs(),
        output_fn=msgs.append,
        detect_theme_fn=lambda: "ansi",
    )
    assert "install bubblewrap" not in "\n".join(msgs).lower()


def test_initial_selection_preselects_claude_code() -> None:
    from imp.cli.init import _initial_selection
    available = ["claude_code", "anthropic", "openai", "codex"]
    assert _initial_selection(available) == {0}


def test_initial_selection_empty_when_claude_code_absent() -> None:
    from imp.cli.init import _initial_selection
    assert _initial_selection(["anthropic", "openai"]) == set()


def test_init_help_describes_what_it_does(capsys: pytest.CaptureFixture[str]) -> None:
    from imp.cli.main import _build_parser
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["init", "-h"])
    # argparse fills the description to the terminal width, so collapse
    # whitespace: a phrase must match wherever the wrap happens to fall.
    help_text = " ".join(capsys.readouterr().out.split())
    assert "default workflow" in help_text
    assert "never makes a git commit" not in help_text
    assert "initial commit" in help_text.lower()
    assert "must name an available workflow" in help_text
    assert "either mode" in help_text


def test_init_scaffolds_commented_remote_driver(tmp_path: Path) -> None:
    run_init(
        project_root=tmp_path, picker_fn=_picks("anthropic"),
        input_fn=_inputs(), output_fn=lambda _msg: None,
    )
    text = (tmp_path / "imphome" / "config.yaml").read_text()
    assert yaml.safe_load(text).get("remote") is None
    assert "# remote:" in text
    assert "#   driver: telegram" in text
    assert "IMP_TELEGRAM_TOKEN" in text and "IMP_TELEGRAM_CHAT_ID" in text


def test_post_init_manifest_notes_the_commit(tmp_path: Path) -> None:
    lines: list[str] = []
    run_init(
        project_root=tmp_path,
        picker_fn=_picks("anthropic"),
        input_fn=_inputs(),
        output_fn=lines.append,
    )
    manifest = "\n".join(lines)
    assert "Committed imp/config.yaml." in manifest
    assert "track in git" not in manifest


def test_already_initialized_with_existing_user_config_claims_no_write(
    tmp_path: Path,
) -> None:
    # Both the committed project file AND the machine global config already exist.
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text("default-workflow: imp\n")
    imp_config_dir = tmp_path / "imphome"
    imp_config_dir.mkdir(parents=True)
    (imp_config_dir / "config.yaml").write_text("providers: [anthropic]\n")

    msgs: list[str] = []
    run_init(
        project_root=tmp_path, picker_fn=_picks("anthropic"),
        input_fn=_inputs(), output_fn=msgs.append,
    )
    text = "\n".join(msgs)
    # Must NOT claim a write that did not happen; global config is untouched.
    assert "bootstrapped" not in text
    assert "already set up" in text
    assert (imp_config_dir / "config.yaml").read_text() == "providers: [anthropic]\n"


def test_init_suggestion_reads_as_opening_message(tmp_path: Path) -> None:
    msgs: list[str] = []
    run_init(
        project_root=tmp_path,
        picker_fn=_picks("anthropic"),
        input_fn=_inputs(),
        output_fn=msgs.append,
        detect_theme_fn=lambda: "ansi",
    )
    blob = "\n".join(msgs)
    assert 'imp new "Add a dark-mode toggle"' in blob
    assert "Your first change" not in blob


def test_picker_cursor_uses_render_safe_caret() -> None:
    from imp.cli.init import _picker_rows

    rows = _picker_rows(["claude_code", "anthropic"], cursor=0, selected={0})
    active = rows[1][1]
    inactive = rows[2][1]
    assert active.startswith("> ")
    assert "❯" not in active
    assert inactive.startswith("  ")


def test_init_finishes_with_no_credential_in_the_environment(tmp_path, monkeypatch):
    # AC 8(a): init is provider-free on every path — no probe, no message.
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        "imp.providers.resolve.available_providers",
        lambda *_a, **_k: pytest.fail("imp init must not probe provider credentials"))
    msgs: list[str] = []
    run_init(tmp_path, picker_fn=_picks("anthropic"), input_fn=_inputs(),
             output_fn=msgs.append)
    assert (tmp_path / "imp" / "config.yaml").is_file()
    assert "usable credential" not in "\n".join(msgs)
    user = tmp_path / "imphome" / "config.yaml"
    assert yaml.safe_load(user.read_text())["providers"] == ["anthropic"]


def test_configured_providers_skip_picker(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "imphome"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.yaml").write_text("providers: [anthropic]\ntheme: dark\n")
    (tmp_path / "src").mkdir()
    _stub_screen(monkeypatch, pythonpath=["src"])

    def _boom(_available):
        raise AssertionError("picker must not run when providers are configured")

    run_init(tmp_path, picker_fn=_boom, input_fn=_inputs(), output_fn=lambda _m: None)
    assert yaml.safe_load(
        (tmp_path / "imp" / "config.yaml").read_text())["pythonpath"] == ["src"]


def test_bwrap_missing_offer_accept_continues(tmp_path, monkeypatch):
    import imp.cli.init as initmod
    monkeypatch.setattr(initmod, "sandbox_available", lambda: False, raising=False)
    (tmp_path / "src").mkdir()
    _stub_screen(monkeypatch, pythonpath=["src"])
    msgs: list[str] = []
    run_init(tmp_path, picker_fn=_picks("anthropic"), input_fn=_inputs("y"),
             output_fn=msgs.append)
    assert yaml.safe_load(
        (tmp_path / "imp" / "config.yaml").read_text())["pythonpath"] == ["src"]
    assert "Running without an OS sandbox." in "\n".join(msgs)
    user = yaml.safe_load((tmp_path / "imphome" / "config.yaml").read_text())
    assert user["sandbox"]["allow_unsandboxed"] is True


def test_bwrap_missing_offer_decline_stops(tmp_path, monkeypatch):
    import imp.cli.init as initmod
    monkeypatch.setattr(initmod, "sandbox_available", lambda: False, raising=False)
    msgs: list[str] = []
    run_init(tmp_path, picker_fn=_picks("anthropic"), input_fn=_inputs("n"),
             output_fn=msgs.append)
    assert not (tmp_path / "imp" / "config.yaml").exists()
    assert "Install bubblewrap for a real filesystem boundary" in "\n".join(msgs)


def test_git_question_accepted_initializes_the_repo(tmp_path_factory, monkeypatch):
    fresh = tmp_path_factory.mktemp("norepo")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(fresh.parent))
    monkeypatch.setenv("IMP_HOME", str(fresh / "imphome"))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")
    run_init(fresh, picker_fn=_picks("anthropic"), input_fn=_inputs(""),
             output_fn=lambda _m: None)
    assert (fresh / ".git").exists()
    assert (fresh / "imp" / "config.yaml").is_file()
    import subprocess
    count = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"], cwd=fresh,
        capture_output=True, text=True, check=True).stdout.strip()
    assert count == "1"


def test_git_question_declined_stops_before_the_screen(
    tmp_path_factory, monkeypatch,
):
    # No repo and the user said no: a clean message and nothing written, never a
    # crash in _commit_init_files' `git status`.
    fresh = tmp_path_factory.mktemp("norepo2")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(fresh.parent))
    monkeypatch.setenv("IMP_HOME", str(fresh / "imphome"))
    msgs: list[str] = []
    run_init(fresh, picker_fn=_picks("anthropic"), input_fn=_inputs("n"),
             output_fn=msgs.append)
    assert not (fresh / "imp" / "config.yaml").exists()
    assert not (fresh / ".git").exists()
    assert "git repository" in "\n".join(msgs).lower()


def test_write_init_files_renders_config_yaml_from_settings(tmp_path: Path) -> None:
    from imp.cli.init import InitSettings, write_init_files
    (tmp_path / "src").mkdir()
    settings = InitSettings(
        default_workflow="light",
        default_base="develop",
        test_cmd_fast="make check",
        worktree_setup="uv sync --all-extras",
        pythonpath=["src"],
    )
    committed = write_init_files(tmp_path, settings)
    data = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    assert data["default-workflow"] == "light"
    assert data["default-base"] == "develop"
    assert data["vars"] == {}
    assert data["test_cmd_fast"] == "make check"
    assert data["worktree_setup"] == "uv sync --all-extras"
    assert data["pythonpath"] == ["src"]
    # Memory scaffold written; commit created for a fresh tree.
    assert (tmp_path / "imp" / "memory" / "all.md").is_file()
    assert committed is True
    # Workflow scaffold uses the chosen workflow, not the hardcoded default.
    assert (tmp_path / "imp" / "memory" / "light" / "all.md").is_file()
    assert not (tmp_path / "imp" / "memory" / "default").exists()
    text = (tmp_path / "imp" / "memory" / "light" / "all.md").read_text()
    assert "`light` workflow" in text
    assert "`default` workflow" not in text


def test_write_init_files_omits_test_block_when_none(tmp_path: Path) -> None:
    from imp.cli.init import InitSettings, write_init_files
    write_init_files(
        tmp_path,
        InitSettings(default_workflow="default", default_base="main",
                     test_cmd_fast=None, worktree_setup=None, pythonpath=None),
    )
    text = (tmp_path / "imp" / "config.yaml").read_text()
    assert "test_cmd_fast" not in text          # None => block omitted entirely
    assert "worktree_setup" not in text         # same reading for the new key
    assert "# pythonpath:" in text              # None => teaching hint
    assert ".imp/" in (tmp_path / ".gitignore").read_text()


def test_write_init_files_writes_the_needs_none_block_for_false(tmp_path: Path) -> None:
    from imp.cli.init import InitSettings, write_init_files
    write_init_files(
        tmp_path,
        InitSettings(default_workflow="default", default_base="main",
                     test_cmd_fast=False, worktree_setup=False, pythonpath=False),
    )
    data = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    assert data["test_cmd_fast"] is False       # decided: this project needs none
    assert data["worktree_setup"] is False
    assert data["pythonpath"] is False


def test_write_init_files_places_worktree_setup_between_tests_and_pythonpath(
    tmp_path: Path,
) -> None:
    from imp.cli.init import InitSettings, write_init_files
    (tmp_path / "src").mkdir()
    write_init_files(
        tmp_path,
        InitSettings(default_workflow="default", default_base="main",
                     test_cmd_fast="make check", worktree_setup="npm ci",
                     pythonpath=["src"]),
    )
    text = (tmp_path / "imp" / "config.yaml").read_text()
    assert (
        "\n# Command run in each new quest worktree before the first step, for toolchains\n"
        "# that keep dependencies in a gitignored per-tree directory (.venv,\n"
        "# node_modules, vendor) that `git worktree` does not materialize. false\n"
        "# declares the project needs none.\n"
        'worktree_setup: "npm ci"\n'
    ) in text
    assert text.index("test_cmd_fast:") < text.index("worktree_setup:")
    assert text.index("worktree_setup:") < text.index("pythonpath:")


def test_use_defaults_detects_the_worktree_setup_command(tmp_path):
    from imp.cli.init import run_init_noninteractive
    (tmp_path / "uv.lock").write_text("")
    run_init_noninteractive(
        tmp_path, output_fn=lambda _m: None, detect_verify_fn=lambda _r: None)
    data = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    # The non-interactive path uses the same detector the screen does.
    assert data["worktree_setup"] == "uv sync --all-extras"


def test_use_defaults_writes_config_yaml_no_model_call(tmp_path, monkeypatch):
    from imp.cli.init import run_init_noninteractive

    def _boom(*_a, **_k):
        raise AssertionError("--use-defaults must make no model/agent/provider call")

    monkeypatch.setattr("imp.providers.resolve.available_providers", _boom)
    run_init_noninteractive(
        tmp_path, output_fn=lambda _m: None, detect_verify_fn=lambda _r: None)
    data = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    assert data["default-workflow"] == "default"
    assert data["default-base"]            # resolved from the repo's current branch
    assert data.get("pythonpath") is None  # no --pythonpath => teaching hint only


def test_use_defaults_override_flags(tmp_path):
    from imp.cli.init import run_init_noninteractive
    (tmp_path / "pkg").mkdir()
    run_init_noninteractive(
        tmp_path, output_fn=lambda _m: None,
        workflow="light", base="develop", pythonpath=["pkg"],
        detect_verify_fn=lambda _r: None)
    data = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    assert data["default-workflow"] == "light"
    assert data["default-base"] == "develop"
    assert data["pythonpath"] == ["pkg"]


def test_use_defaults_pythonpath_drops_absent_dirs(tmp_path):
    from imp.cli.init import run_init_noninteractive
    (tmp_path / "real").mkdir()
    run_init_noninteractive(
        tmp_path, output_fn=lambda _m: None,
        pythonpath=["real", "ghost"], detect_verify_fn=lambda _r: None)
    data = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    assert data["pythonpath"] == ["real"]   # validated against disk


def test_use_defaults_all_invalid_pythonpath_reports_and_leaves_unset(tmp_path):
    from imp.cli.init import run_init_noninteractive
    msgs: list[str] = []
    run_init_noninteractive(
        tmp_path, output_fn=msgs.append,
        pythonpath=["srcc"], detect_verify_fn=lambda _r: None)
    # config must NOT record pythonpath: false — that is the deliberate opt-out
    data = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    assert data.get("pythonpath") is None
    # a report line must name the bad entry and say roots are left unset
    report = "\n".join(msgs)
    assert "srcc" in report
    assert "no such directory" in report
    assert "leaving import roots unset" in report


def test_use_defaults_fills_offline_test_command(tmp_path):
    from imp.cli.init import run_init_noninteractive
    from imp.cli.verify_detect import DetectedTestCommand
    run_init_noninteractive(
        tmp_path, output_fn=lambda _m: None,
        # heuristic hit, no prompt
        detect_verify_fn=lambda _r: DetectedTestCommand("make check", "from Makefile"))
    data = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    assert data["test_cmd_fast"] == "make check"


def test_use_defaults_detects_pythonpath_from_pyproject(tmp_path):
    from imp.cli.init import run_init_noninteractive
    (tmp_path / "pyproject.toml").write_text(
        '[tool.hatch.build.targets.wheel]\npackages = ["src/app"]\n')
    (tmp_path / "src" / "app").mkdir(parents=True)
    run_init_noninteractive(
        tmp_path, output_fn=lambda _m: None, detect_verify_fn=lambda _r: None)
    data = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    assert data["pythonpath"] == ["src"]


def test_use_defaults_explicit_pythonpath_overrides_detection(tmp_path):
    from imp.cli.init import run_init_noninteractive
    (tmp_path / "pyproject.toml").write_text(
        '[tool.hatch.build.targets.wheel]\npackages = ["src/app"]\n')
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "vendor").mkdir()
    run_init_noninteractive(
        tmp_path, output_fn=lambda _m: None, pythonpath=["vendor"],
        detect_verify_fn=lambda _r: None)
    data = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    assert data["pythonpath"] == ["vendor"]   # the flag wins over detection


def test_use_defaults_false_pythonpath_suppresses_detection(tmp_path):
    from imp.cli.init import run_init_noninteractive
    # Same fixture that triggers detection in test_use_defaults_detects_pythonpath_from_pyproject
    (tmp_path / "pyproject.toml").write_text(
        '[tool.hatch.build.targets.wheel]\npackages = ["src/app"]\n')
    (tmp_path / "src" / "app").mkdir(parents=True)
    # pythonpath=False is the deliberate opt-out — the detector must NOT run
    run_init_noninteractive(
        tmp_path, output_fn=lambda _m: None, pythonpath=False,
        detect_verify_fn=lambda _r: None)
    text = (tmp_path / "imp" / "config.yaml").read_text()
    assert yaml.safe_load(text)["pythonpath"] is False   # not ["src"], not the hint
    assert "# pythonpath:" not in text


def test_use_defaults_initializes_missing_git_repo(tmp_path_factory, monkeypatch):
    # A truly repo-less dir OUTSIDE the autouse _git_repo fixture's repo (which
    # git-inits tmp_path, so any tmp_path subdir is already inside a repo). Cap
    # git's upward repo discovery at the temp base so no ancestor repo is found;
    # global config stays isolated via the autouse _isolate_global_config fixture.
    from imp.cli.init import run_init_noninteractive
    fresh = tmp_path_factory.mktemp("fresh")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(fresh.parent))
    run_init_noninteractive(fresh, output_fn=lambda _m: None,
                            detect_verify_fn=lambda _r: None)
    assert (fresh / ".git").exists()
    assert (fresh / "imp" / "config.yaml").is_file()


def test_use_defaults_idempotent_on_existing_clone(tmp_path):
    from imp.cli.init import run_init_noninteractive
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text("default-workflow: imp\n")
    msgs: list[str] = []
    run_init_noninteractive(tmp_path, output_fn=msgs.append,
                            detect_verify_fn=lambda _r: None)
    assert (tmp_path / "imp" / "config.yaml").read_text() == "default-workflow: imp\n"
    assert "already initialized" in "\n".join(msgs).lower()


def test_use_defaults_idempotent_in_non_git_dir(tmp_path_factory, monkeypatch):
    # Re-init on a copied imp/config.yaml sitting in a dir with NO git repo must
    # not raise: the already-initialized branch ensures a repo before committing.
    from imp.cli.init import run_init_noninteractive
    fresh = tmp_path_factory.mktemp("copied")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(fresh.parent))
    (fresh / "imp").mkdir()
    (fresh / "imp" / "config.yaml").write_text("default-workflow: imp\n")
    run_init_noninteractive(fresh, output_fn=lambda _m: None,
                            detect_verify_fn=lambda _r: None)
    assert (fresh / "imp" / "config.yaml").read_text() == "default-workflow: imp\n"
    assert (fresh / ".git").exists()


def test_use_defaults_adopts_current_branch_without_origin_head(tmp_path):
    import subprocess

    from imp.cli.init import run_init_noninteractive

    # A born repo with no origin remote and no --base override: base resolution
    # is prompt-free now, so it adopts the checked-out branch instead of raising.
    subprocess.run(["git", "commit", "--allow-empty", "-m", "seed", "-q"],
                   cwd=tmp_path, check=True)
    current = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=tmp_path, capture_output=True, text=True, check=True).stdout.strip()
    run_init_noninteractive(tmp_path, output_fn=lambda _m: None,
                            detect_verify_fn=lambda _r: None)
    data = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    assert data["default-base"] == current


def test_use_defaults_raises_on_detached_head(tmp_path):
    import subprocess

    from imp.cli.init import IntegrationBranchUnresolved, run_init_noninteractive

    subprocess.run(["git", "commit", "--allow-empty", "-m", "seed", "-q"],
                   cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "--detach", "-q"], cwd=tmp_path, check=True)
    with pytest.raises(IntegrationBranchUnresolved) as excinfo:
        run_init_noninteractive(tmp_path, output_fn=lambda _m: None,
                                detect_verify_fn=lambda _r: None)
    assert str(excinfo.value) == (
        "imp init couldn't determine an integration branch (HEAD is detached). "
        "Re-run with `--base <branch>`, or add `default-base: <branch>` to "
        "imp/config.yaml.")


def test_init_parser_maps_use_defaults_to_non_interactive() -> None:
    from imp.cli.main import _build_parser
    args = _build_parser().parse_args(["init", "--use-defaults"])
    assert args.non_interactive is True
    args2 = _build_parser().parse_args(["init"])
    assert args2.non_interactive is False


def test_init_parser_accepts_override_flags() -> None:
    from imp.cli.main import _build_parser
    args = _build_parser().parse_args(
        ["init", "--workflow", "light", "--base", "develop",
         "--pythonpath", "src,lib"])
    assert args.workflow == "light"
    assert args.base == "develop"
    assert args.pythonpath == "src,lib"


def test_init_no_tty_routes_non_interactive(tmp_path, monkeypatch):
    # No --use-defaults, but a non-tty stdin/stdout must auto-route to the
    # provider-free path (no hang, no picker).
    import imp.cli.main as m

    monkeypatch.setattr(m, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(m.sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(m.sys.stdout, "isatty", lambda: False, raising=False)

    called: dict[str, object] = {}

    def _spy(project, **kw):
        called["project"] = project
        called["kw"] = kw

    monkeypatch.setattr("imp.cli.init.run_init_noninteractive", _spy)
    monkeypatch.setattr("imp.cli.init.run_init",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("interactive path must not run without a tty")))
    args = m._build_parser().parse_args(["init", "--pythonpath", "src, ,lib"])
    args.func(args)
    assert called["project"] == tmp_path
    assert called["kw"]["pythonpath"] == ["src", "lib"]   # comma-split, blanks dropped


def test_init_empty_pythonpath_flag_passes_false(tmp_path, monkeypatch):
    import imp.cli.main as m

    monkeypatch.setattr(m, "_project_root", lambda: tmp_path)
    called: dict[str, object] = {}

    def _spy(project, **kw):
        called["kw"] = kw

    monkeypatch.setattr("imp.cli.init.run_init_noninteractive", _spy)

    # --pythonpath "" is an explicit instruction: the opt-out, not "detect"
    args = m._build_parser().parse_args(["init", "--use-defaults", "--pythonpath", ""])
    args.func(args)
    assert called["kw"]["pythonpath"] is False

    # absent --pythonpath must yield pythonpath=None
    called.clear()
    args2 = m._build_parser().parse_args(["init", "--use-defaults"])
    args2.func(args2)
    assert called["kw"]["pythonpath"] is None


def _init_args(**over: object) -> argparse.Namespace:
    """A parser-shaped Namespace for _cmd_init: --use-defaults => non_interactive."""
    fields: dict[str, object] = {
        "non_interactive": True, "workflow": None, "base": None, "pythonpath": None}
    fields.update(over)
    return argparse.Namespace(**fields)


def _expected_unknown_workflow_line(project_root: Path, name: str) -> str:
    from imp.assets import selectable_workflow_names
    names = ", ".join(selectable_workflow_names(project_root))
    return f"imp: unknown workflow {name!r}; available: {names}"


def test_cmd_init_rejects_unknown_workflow_before_writing(tmp_path, monkeypatch, capsys):
    import imp.cli.main as m

    monkeypatch.setattr(m, "_project_root", lambda: tmp_path)
    monkeypatch.setattr("imp.cli.init.run_init_noninteractive",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("a typo'd --workflow must not reach the writer")))
    rc = m._cmd_init(_init_args(workflow="minimial"))
    captured = capsys.readouterr()

    assert rc == 2
    assert captured.err.splitlines() == [
        _expected_unknown_workflow_line(tmp_path, "minimial")]
    assert captured.out == ""
    assert not (tmp_path / "imp").exists()          # no config.yaml, no memory scaffold


def test_cmd_init_rejects_internal_memory_workflow(tmp_path, monkeypatch, capsys):
    import imp.cli.main as m

    monkeypatch.setattr(m, "_project_root", lambda: tmp_path)
    monkeypatch.setattr("imp.cli.init.run_init_noninteractive",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("memory is not selectable as a project default")))
    rc = m._cmd_init(_init_args(workflow="memory"))
    err = capsys.readouterr().err

    assert rc == 2
    assert err.splitlines() == [_expected_unknown_workflow_line(tmp_path, "memory")]
    assert "memory" not in err.split("available: ")[1]
    assert not (tmp_path / "imp").exists()


def test_cmd_init_rejects_bad_workflow_on_the_interactive_path(
    tmp_path, monkeypatch, capsys
):
    # No --use-defaults and both streams a tty => the interactive branch. The flag
    # used to be silently ignored here; it must now be rejected before any prompt.
    import imp.cli.main as m

    monkeypatch.setattr(m, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(m.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(m.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("imp.cli.init.run_init",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("setup must not start with a bad --workflow")))
    rc = m._cmd_init(_init_args(non_interactive=False, workflow="minimial"))

    assert rc == 2
    assert capsys.readouterr().err.splitlines() == [
        _expected_unknown_workflow_line(tmp_path, "minimial")]
    assert not (tmp_path / "imp").exists()


def test_cmd_init_tolerates_a_namespace_without_the_workflow_attr(
    tmp_path, monkeypatch
):
    import imp.cli.main as m

    monkeypatch.setattr(m, "_project_root", lambda: tmp_path)
    monkeypatch.setattr("imp.cli.init.run_init_noninteractive", lambda project, **kw: None)
    # No `workflow` attribute at all — the shape existing _cmd_init tests build.
    assert m._cmd_init(argparse.Namespace(non_interactive=True)) == 0


def test_cmd_init_use_defaults_writes_the_selected_workflow(tmp_path, monkeypatch, capsys):
    import imp.cli.main as m

    monkeypatch.setattr(m, "_project_root", lambda: tmp_path)
    rc = m._cmd_init(_init_args(workflow="minimal"))
    captured = capsys.readouterr()

    assert rc == 0
    assert captured.err == ""
    data = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    assert data["default-workflow"] == "minimal"
    assert (tmp_path / "imp" / "memory" / "minimal" / "all.md").is_file()


def test_cmd_init_use_defaults_without_flag_keeps_the_default_workflow(
    tmp_path, monkeypatch, capsys
):
    import imp.cli.main as m

    monkeypatch.setattr(m, "_project_root", lambda: tmp_path)
    rc = m._cmd_init(_init_args())
    captured = capsys.readouterr()

    assert rc == 0
    assert captured.err == ""
    data = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    assert data["default-workflow"] == "default"
    assert (tmp_path / "imp" / "memory" / "default" / "all.md").is_file()


def test_fresh_init_applies_the_detected_theme_before_the_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.cli import palette as pal

    monkeypatch.delenv("IMP_THEME", raising=False)
    seen: dict[str, str] = {}
    _stub_screen_recording_theme(monkeypatch, seen)

    run_init(
        project_root=tmp_path, picker_fn=_picks("anthropic"),
        input_fn=_inputs(), output_fn=lambda _m: None,
        detect_theme_fn=lambda: "dark",
    )

    assert seen["theme"] == "dark"
    # the process palette really emits the dark truecolor gold, not 16-color yellow
    assert pal.ansi("x", "wordmark", enabled=True) == "\x1b[38;2;255;154;60mx\x1b[0m"


def test_init_does_not_clobber_an_imp_theme_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.cli import palette as pal

    monkeypatch.setenv("IMP_THEME", "light")
    pal.set_theme("light")  # as _apply_theme already did at startup
    seen: dict[str, str] = {}
    _stub_screen_recording_theme(monkeypatch, seen)

    run_init(
        project_root=tmp_path, picker_fn=_picks("anthropic"),
        input_fn=_inputs(), output_fn=lambda _m: None,
        detect_theme_fn=lambda: "dark",
    )

    assert seen["theme"] == "light"
    assert pal.active_theme() == "light"


def test_provider_picker_runs_before_the_theme_is_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.cli import palette as pal

    monkeypatch.delenv("IMP_THEME", raising=False)
    seen: dict[str, str] = {}

    def _pick(_available):
        seen["theme"] = pal.active_theme()
        return ["anthropic"]

    run_init(
        project_root=tmp_path, picker_fn=_pick,
        input_fn=_inputs(), output_fn=lambda _m: None,
        detect_theme_fn=lambda: "dark",
    )

    assert seen["theme"] == "ansi"


GOLD_DARK = "\x1b[38;2;255;154;60m"
GOLD_ANSI = "\x1b[33m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"


def _colored_fresh_init(tmp_path, monkeypatch, msgs: list[str]) -> None:
    # conftest resets the palette but not the ambient color/theme env vars.
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("IMP_THEME", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    run_init(
        project_root=tmp_path, picker_fn=_picks("anthropic"),
        input_fn=_inputs(), output_fn=msgs.append,
        detect_theme_fn=lambda: "dark",
    )


def test_host_success_lines_carry_engine_chrome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.providers.paths import global_config_path

    msgs: list[str] = []
    _colored_fresh_init(tmp_path, monkeypatch, msgs)
    user_path = global_config_path()

    # This one prints before the theme is applied, so its gold is the ansi gold.
    assert f"Setting up imp in {GOLD_ANSI}{tmp_path}{RESET}" in msgs
    assert f"{DIM}Enabled: anthropic{RESET}" in msgs
    assert f"{DIM}  anthropic — set ANTHROPIC_API_KEY{RESET}" in msgs
    assert f"{DIM}Detected a dark terminal — colors tuned to match.{RESET}" in msgs
    assert f"{GOLD_DARK}Created:{RESET}" in msgs
    assert f"{DIM}  imp/config.yaml{RESET}" in msgs
    assert f"{DIM}  {user_path}  (your machine: providers, theme){RESET}" in msgs
    assert f"{DIM}Committed imp/config.yaml.{RESET}" in msgs
    assert f'Try: {GOLD_DARK}imp new "Add a dark-mode toggle"{RESET}' in msgs


def test_host_success_lines_stay_bare_without_color(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    msgs: list[str] = []

    run_init(
        project_root=tmp_path, picker_fn=_picks("anthropic"),
        input_fn=_inputs(), output_fn=msgs.append,
        detect_theme_fn=lambda: "dark",
    )

    assert f"Setting up imp in {tmp_path}" in msgs
    assert "Created:" in msgs
    assert not any("\x1b" in m for m in msgs)


def test_the_hand_off_line_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    msgs: list[str] = []

    run_init(
        project_root=tmp_path, picker_fn=_picks("anthropic"),
        input_fn=_inputs(), output_fn=msgs.append,
    )

    assert not any("Now let's finish setting up this project." in m for m in msgs)


def test_already_initialized_rerun_block_stays_bare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.providers.paths import global_config_path

    monkeypatch.setenv("FORCE_COLOR", "1")
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text("default-workflow: imp\n")
    cfg = global_config_path()
    cfg.parent.mkdir(parents=True)
    cfg.write_text("providers: [anthropic]\ntheme: dark\n")

    msgs: list[str] = []
    run_init(
        project_root=tmp_path, picker_fn=_picks("anthropic"),
        input_fn=_inputs(), output_fn=msgs.append,
    )

    user_path = global_config_path()
    assert "Project already initialized (imp/config.yaml is committed)." in msgs
    # the same two strings the fresh path chromes — bare here
    assert f"  {user_path}  (your machine: providers, theme)" in msgs
    assert "Committed imp/config.yaml." in msgs


def test_noninteractive_output_stays_bare_under_forced_color(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.cli.init import run_init_noninteractive

    monkeypatch.setenv("FORCE_COLOR", "1")
    msgs: list[str] = []
    run_init_noninteractive(
        tmp_path, output_fn=msgs.append, detect_verify_fn=lambda _r: None)

    assert "Created:" in msgs
    assert "  imp/config.yaml" in msgs
    assert not any("\x1b" in m for m in msgs)


def test_already_initialized_rerun_leaves_the_palette_alone(tmp_path: Path) -> None:
    from imp.cli import palette as pal
    from imp.providers.paths import global_config_path

    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text("default-workflow: imp\n")
    cfg = global_config_path()
    cfg.parent.mkdir(parents=True)
    cfg.write_text("providers: [anthropic]\ntheme: dark\n")

    run_init(
        project_root=tmp_path, picker_fn=_picks("anthropic"),
        input_fn=_inputs(), output_fn=lambda _m: None,
    )

    assert pal.active_theme() == "ansi"


def _stub_splash(monkeypatch, seen: dict) -> None:
    import imp.cli.init as initmod

    def _fake(*, theme, width, color):
        seen["theme"] = theme
        seen["width"] = width
        seen["color"] = color
        return "SPLASH-BLOCK"

    monkeypatch.setattr(initmod.splash, "splash_block", _fake)


def test_splash_prints_once_in_the_resolved_theme_before_the_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.init as initmod

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("IMP_THEME", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    seen: dict[str, object] = {}
    _stub_splash(monkeypatch, seen)
    msgs: list[str] = []
    at_screen: dict[str, bool] = {}

    def _screen(_root, state, **_kw):
        at_screen["splash_already_printed"] = "SPLASH-BLOCK" in msgs
        return initmod.ScreenAccepted(state)

    monkeypatch.setattr(initmod, "_interactive_confirm_screen", _screen,
                        raising=False)

    run_init(
        project_root=tmp_path, picker_fn=_picks("anthropic"),
        input_fn=_inputs(), output_fn=msgs.append,
        detect_theme_fn=lambda: "dark",
    )

    assert msgs.count("SPLASH-BLOCK") == 1
    assert seen["theme"] == "dark"
    assert seen["color"] is True
    assert at_screen["splash_already_printed"] is True
    i = msgs.index("SPLASH-BLOCK")
    assert msgs[i - 1] == ""      # one blank above
    assert msgs[i - 2] != ""      # never two
    # the screen draws and erases itself here; one blank, then the settings block
    assert msgs[i + 1] == ""
    assert "Configured:" in msgs[i + 2]
    created_idx = next(j for j, m in enumerate(msgs) if "Created:" in m)
    assert created_idx > i + 2


def test_no_splash_without_color_but_one_blank_before_the_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.init as initmod

    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    seen: dict[str, object] = {}
    _stub_splash(monkeypatch, seen)
    msgs: list[str] = []
    tail: dict[str, list[str]] = {}

    def _screen(_root, state, **_kw):
        tail["msgs"] = list(msgs)
        return initmod.ScreenAccepted(state)

    monkeypatch.setattr(initmod, "_interactive_confirm_screen", _screen,
                        raising=False)

    run_init(
        project_root=tmp_path, picker_fn=_picks("anthropic"),
        input_fn=_inputs(), output_fn=msgs.append,
        detect_theme_fn=lambda: "dark",
    )

    assert seen == {}                    # splash_block never called
    assert tail["msgs"][-1] == ""        # exactly one blank …
    assert tail["msgs"][-2] != ""        # … before the screen opens


def test_already_initialized_rerun_prints_no_splash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    seen: dict[str, object] = {}
    _stub_splash(monkeypatch, seen)
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text("default-workflow: imp\n")

    msgs: list[str] = []
    run_init(
        project_root=tmp_path, picker_fn=_picks("anthropic"),
        input_fn=_inputs(), output_fn=msgs.append,
        detect_theme_fn=lambda: "dark",
    )

    assert seen == {}
    assert "SPLASH-BLOCK" not in msgs


def test_picker_cursor_row_is_gold_not_cyan() -> None:
    from imp.cli.init import _picker_rows

    rows = _picker_rows(["claude_code", "anthropic"], cursor=0, selected={0})
    assert rows[1][0] == "ansiyellow bold"
    assert rows[2][0] == ""


def test_picker_block_has_a_blank_line_around_the_options() -> None:
    from imp.cli.init import _picker_rows

    rows = _picker_rows(["claude_code", "anthropic"], cursor=0, selected={0})
    text = "".join(t for _style, t in rows)
    assert text.splitlines() == [
        "Which providers do you want to enable?",
        "",
        "> [x] claude_code",
        "  [ ] anthropic",
        "",
        "↑↓ move · space toggle · enter confirm · esc cancel",
    ]


def test_picker_docstring_no_longer_calls_the_cursor_row_cyan() -> None:
    from imp.cli.init import _interactive_provider_picker

    doc = _interactive_provider_picker.__doc__ or ""
    assert "ansicyan" not in doc
    assert "ansiyellow" in doc


def test_read_user_config_refuses_unparseable_yaml(tmp_path: Path) -> None:
    # Reading a hand-broken machine config as "providers absent" would make init
    # repair it — i.e. hand corrupt text to the writer. Report it instead.
    from imp.cli.config_write import ConfigWriteError
    from imp.cli.init import _read_user_config

    p = tmp_path / "config.yaml"
    p.write_text("providers: [anthropic\n")
    with pytest.raises(ConfigWriteError) as exc:
        _read_user_config(p)
    assert str(exc.value).startswith("invalid YAML in config.yaml: ")


def test_read_user_config_refuses_a_non_mapping(tmp_path: Path) -> None:
    """Valid YAML that is not a mapping used to flow through and blow up on
    `.get("providers")` — an uncaught traceback out of `imp init`."""
    from imp.cli.config_write import ConfigWriteError
    from imp.cli.init import _read_user_config

    p = tmp_path / "config.yaml"
    p.write_text("- claude_code\n- openai\n")
    with pytest.raises(ConfigWriteError) as exc:
        _read_user_config(p)
    assert str(exc.value) == "config.yaml must be a mapping of settings, got list"


def test_cmd_init_refuses_on_detached_head(tmp_path, monkeypatch, capsys):
    import imp.cli.main as m
    from imp.engine.worktree import checked_out_branch

    _init_repo(tmp_path)
    _git(tmp_path, "checkout", "-q", "--detach")
    head_before = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.setattr(m, "_project_root", lambda: tmp_path)
    monkeypatch.setattr("imp.cli.init.run_init_noninteractive",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("a detached HEAD must not reach the writer")))

    # A bad --workflow too: the detached check must be the one that reports.
    rc = m._cmd_init(_init_args(workflow="minimial"))
    captured = capsys.readouterr()

    assert rc == 2
    assert captured.err.splitlines() == [
        "imp: HEAD is detached; check out a branch before `imp init` "
        "(it commits imp's config)."]
    assert captured.out == ""
    assert not (tmp_path / "imp").exists()                    # wrote nothing
    assert _git(tmp_path, "rev-parse", "HEAD").stdout.strip() == head_before
    assert checked_out_branch(tmp_path) is None               # still detached


def test_cmd_init_runs_on_an_unborn_branch(tmp_path, monkeypatch, capsys):
    import imp.cli.main as m

    _git(tmp_path, "init", "-q", "-b", "main")   # repo, no commits
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    monkeypatch.setattr(m, "_project_root", lambda: tmp_path)

    rc = m._cmd_init(_init_args(workflow="minimal"))

    assert rc == 0
    assert capsys.readouterr().err == ""
    assert (tmp_path / "imp" / "config.yaml").is_file()
    assert _git(tmp_path, "log", "--format=%s").stdout.splitlines() == [
        "imp: initialize project"]


def test_cmd_init_runs_in_a_fresh_directory(tmp_path_factory, monkeypatch, capsys):
    # A repo-less dir OUTSIDE the autouse _git_repo fixture's repo; git's upward
    # discovery is capped so no ancestor repo is found.
    import imp.cli.main as m

    fresh = tmp_path_factory.mktemp("fresh")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(fresh.parent))
    monkeypatch.setattr(m, "_project_root", lambda: fresh)

    rc = m._cmd_init(_init_args(workflow="minimal"))

    assert rc == 0
    assert capsys.readouterr().err == ""
    assert (fresh / ".git").exists()
    assert (fresh / "imp" / "config.yaml").is_file()


def test_picker_confirmation_hints_every_enabled_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """claude_code gets a hint now — it used to be silently skipped for having no env var."""
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    msgs: list[str] = []

    run_init(
        project_root=tmp_path, picker_fn=_picks("claude_code", "anthropic"),
        input_fn=_inputs(), output_fn=msgs.append,
        detect_theme_fn=lambda: "ansi",
    )

    enabled = msgs.index("Enabled: claude_code, anthropic")
    assert msgs[enabled + 1] == "  claude_code — run `claude login`"
    assert msgs[enabled + 2] == "  anthropic — set ANTHROPIC_API_KEY"
    assert not any("set this env var" in m for m in msgs)


def test_string_built_env_hints_are_gone_from_the_source_tree() -> None:
    src = Path(__file__).resolve().parents[1] / "src"
    blob = "\n".join(p.read_text() for p in src.rglob("*.py"))
    assert "_PROVIDERS_WITHOUT_ENV" not in blob
    assert "(set this env var)" not in blob


def test_interactive_init_refuses_an_unparseable_machine_config(tmp_path: Path) -> None:
    from imp.cli.config_write import ConfigWriteError
    from imp.cli.init import run_init

    home = tmp_path / "imphome"
    home.mkdir(parents=True)
    broken = "providers: [anthropic\n"
    (home / "config.yaml").write_text(broken)

    with pytest.raises(ConfigWriteError):
        run_init(
            project_root=tmp_path, picker_fn=_picks("claude_code"),
            input_fn=_inputs(), output_fn=lambda _m: None,
        )

    assert (home / "config.yaml").read_text() == broken
    assert not (tmp_path / "imp").exists()


def test_cmd_init_reports_an_unparseable_machine_config_interactively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Both streams a tty + no --use-defaults => _select_headless picks the
    # interactive branch (src/imp/cli/main.py); under pytest
    # stdin.isatty() is False, so the patches are what reach run_init at all.
    import imp.cli.main as m

    _init_repo(tmp_path)
    home = tmp_path / "imphome"
    home.mkdir(parents=True)
    broken = "providers: [anthropic\n"
    (home / "config.yaml").write_text(broken)
    monkeypatch.setattr(m, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(m.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(m.sys.stdout, "isatty", lambda: True, raising=False)
    # _cmd_init injects no picker_fn, so pin the real terminal picker shut: the
    # refusal must land before it, and an unpinned red run would try to draw it.
    monkeypatch.setattr(
        "imp.cli.init._interactive_provider_picker",
        lambda _available: (_ for _ in ()).throw(
            AssertionError("a config that will not parse must not reach the picker")))

    rc = m._cmd_init(_init_args(non_interactive=False))

    captured = capsys.readouterr()
    assert rc == 2
    # `imp: ` prefixes the first line only; ruamel's parser detail is itself a
    # multi-line snippet, so pin the head, not the message's line count.
    assert captured.err.splitlines()[0].startswith(
        "imp: invalid YAML in config.yaml: ")
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out
    assert (home / "config.yaml").read_text() == broken
    assert not (tmp_path / "imp" / "config.yaml").exists()


def _seed_machine_config(home: Path, providers_block: str) -> str:
    """Real bootstrapped machine bytes (comments, theme, editing_mode, sandbox
    block, remote hint) with the providers key swapped for `providers_block`."""
    from imp.cli.init import _bootstrap_user_config

    path = home / "config.yaml"
    _bootstrap_user_config(path, ["anthropic"], "dark", allow_unsandboxed=True)
    text = path.read_text().replace("providers:\n- anthropic\n", providers_block)
    path.write_text(text)
    return text


def _sans_providers(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if not ln.startswith("providers:")]


def test_set_user_providers_replaces_an_empty_list_and_preserves_the_file(
    tmp_path: Path,
) -> None:
    from imp.cli.init import _set_user_providers

    home = tmp_path / "imphome"
    home.mkdir(parents=True)
    before = _seed_machine_config(home, "providers: []\n")

    _set_user_providers(home / "config.yaml", ["claude_code", "openai"])

    after = (home / "config.yaml").read_text()
    assert "providers: [claude_code, openai]" in after
    assert _sans_providers(after) == _sans_providers(before)
    data = yaml.safe_load(after)
    assert data["theme"] == "dark"
    assert data["editing_mode"] == "emacs"
    assert data["sandbox"] == {"allow_unsandboxed": True}
    assert "# remote:" in after


def test_set_user_providers_inserts_an_absent_key(tmp_path: Path) -> None:
    from imp.cli.init import _set_user_providers

    home = tmp_path / "imphome"
    home.mkdir(parents=True)
    before = _seed_machine_config(home, "")
    assert "providers" not in yaml.safe_load(before)

    _set_user_providers(home / "config.yaml", ["claude_code"])

    after = (home / "config.yaml").read_text()
    assert yaml.safe_load(after)["providers"] == ["claude_code"]
    assert _sans_providers(after) == _sans_providers(before)


def test_set_user_providers_refuses_a_write_that_does_not_read_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read-back gate is the writer's whole point: a candidate imp's own
    reader does not report back must never land."""
    from imp.cli.config_write import ConfigWriteError
    from imp.cli.init import _set_user_providers

    home = tmp_path / "imphome"
    home.mkdir(parents=True)
    before = _seed_machine_config(home, "providers: []\n")
    monkeypatch.setattr("imp.cli.config_write.verify", lambda *_a, **_kw: False)

    with pytest.raises(ConfigWriteError) as exc:
        _set_user_providers(home / "config.yaml", ["claude_code"])

    assert "would not read back as given" in str(exc.value)
    assert (home / "config.yaml").read_text() == before


@pytest.mark.parametrize(
    "raw",
    [
        None,                            # key absent
        [],                              # the state init used to write
        "claude_code",                   # hand-edited scalar: iterating yields chars
        ["claude_code", 7],              # _parse_providers rejects a non-string entry
        ["codex", "codex"],              # _parse_providers rejects a duplicate entry
    ],
)
def test_no_usable_providers_to_reuse(raw: object) -> None:
    from imp.cli.init import _configured_providers

    assert _configured_providers({} if raw is None else {"providers": raw}) is None
    assert _configured_providers(None) is None


def test_configured_providers_returns_a_usable_list_verbatim() -> None:
    from imp.cli.init import _configured_providers

    assert _configured_providers({"providers": ["openai", "codex"]}) == [
        "openai", "codex"]


@pytest.mark.parametrize(
    "seed",
    ["providers: []\n", "", "providers: claude_code\n",
     "providers: [codex, codex]\n"])
def test_interactive_reinit_repairs_an_unusable_provider_list(
    tmp_path: Path, seed: str,
) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text("default-workflow: imp\n")
    home = tmp_path / "imphome"
    home.mkdir(parents=True)
    before = _seed_machine_config(home, seed)

    msgs: list[str] = []
    run_init(
        project_root=tmp_path,
        picker_fn=_picks("claude_code", "anthropic", "openai", "codex"),
        input_fn=_inputs(), output_fn=msgs.append,
    )

    after = (home / "config.yaml").read_text()
    assert yaml.safe_load(after)["providers"] == [
        "claude_code", "anthropic", "openai", "codex"]
    assert _sans_providers(after) == _sans_providers(before)
    assert ("Set providers in your machine config: "
            "claude_code, anthropic, openai, codex.") in msgs
    assert not any("already set up" in m for m in msgs)


def test_interactive_reinit_reuses_a_valid_list_and_names_the_setter(
    tmp_path: Path,
) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text("default-workflow: imp\n")
    home = tmp_path / "imphome"
    home.mkdir(parents=True)
    before = _seed_machine_config(home, "providers: [anthropic, openai]\n")

    def _boom(_available: list[str]) -> list[str]:
        raise AssertionError("picker must not run when providers are configured")

    msgs: list[str] = []
    run_init(
        project_root=tmp_path, picker_fn=_boom,
        input_fn=_inputs(), output_fn=msgs.append,
    )

    assert (home / "config.yaml").read_text() == before   # never silently rewritten
    assert "Your machine config is already set up — nothing to do." in msgs
    path_line = msgs.index(
        f"  {home / 'config.yaml'}  (your machine: providers, theme)")
    assert msgs[path_line + 1] == (
        "Change providers later: imp config set providers <name>,<name>")


_DEFAULT_PROVIDERS = ["claude_code", "anthropic", "openai", "codex"]


def test_use_defaults_writes_the_default_preference_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_registry: object,
) -> None:
    # provider_registry (tests/conftest.py) resets the process-global registry on
    # BOTH sides; resolve._ensure_builtins then lazily re-registers the builtins
    # this test reads through. Registering here by hand would leak to every later
    # test in the worker.
    from imp.cli.init import run_init_noninteractive
    from imp.cli.main import main
    from imp.providers.effective import load_effective_config

    msgs: list[str] = []

    run_init_noninteractive(
        tmp_path, output_fn=msgs.append, detect_verify_fn=lambda _r: None)

    data = yaml.safe_load((tmp_path / "imphome" / "config.yaml").read_text())
    assert data["providers"] == _DEFAULT_PROVIDERS
    branch_line = next(
        i for i, m in enumerate(msgs) if m.startswith("Integration branch: "))
    assert msgs[branch_line + 1] == (
        "Providers: claude_code, anthropic, openai, codex "
        "(the first authenticated one is used)")
    # imp's own readers must accept what init wrote — `providers: []` did not.
    assert list(load_effective_config(control_root=tmp_path).config.providers) == (
        _DEFAULT_PROVIDERS)
    monkeypatch.chdir(tmp_path)
    assert main(["config"]) == 0


@pytest.mark.parametrize(
    "seed",
    ["providers: []\n", "", "providers: claude_code\n",
     "providers: [codex, codex]\n"])
def test_use_defaults_repairs_an_existing_machine_config(
    tmp_path: Path, seed: str,
) -> None:
    from imp.cli.init import run_init_noninteractive

    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text("default-workflow: imp\n")
    home = tmp_path / "imphome"
    home.mkdir(parents=True)
    before = _seed_machine_config(home, seed)

    msgs: list[str] = []
    run_init_noninteractive(
        tmp_path, output_fn=msgs.append, detect_verify_fn=lambda _r: None)

    after = (home / "config.yaml").read_text()
    assert yaml.safe_load(after)["providers"] == _DEFAULT_PROVIDERS
    assert _sans_providers(after) == _sans_providers(before)
    assert msgs == [
        "Project already initialized (imp/config.yaml is committed).",
        "Set providers in your machine config: "
        "claude_code, anthropic, openai, codex.",
    ]


def test_use_defaults_says_nothing_about_providers_when_it_repairs_nothing(
    tmp_path: Path,
) -> None:
    from imp.cli.init import run_init_noninteractive

    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text("default-workflow: imp\n")
    home = tmp_path / "imphome"
    home.mkdir(parents=True)
    before = _seed_machine_config(home, "providers: [anthropic]\n")

    msgs: list[str] = []
    run_init_noninteractive(
        tmp_path, output_fn=msgs.append, detect_verify_fn=lambda _r: None)

    assert (home / "config.yaml").read_text() == before
    assert msgs == ["Project already initialized (imp/config.yaml is committed)."]


def test_cmd_init_reports_an_unparseable_machine_config_non_interactively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The scripted path reads the machine config too — before it writes
    anything, so a file it cannot parse aborts init with the tree untouched."""
    import imp.cli.main as m

    _init_repo(tmp_path)
    home = tmp_path / "imphome"
    home.mkdir(parents=True)
    broken = "providers: [anthropic\n"
    (home / "config.yaml").write_text(broken)
    monkeypatch.setattr(m, "_project_root", lambda: tmp_path)

    rc = m._cmd_init(_init_args())   # --use-defaults => the headless branch

    captured = capsys.readouterr()
    assert rc == 2
    assert captured.err.splitlines()[0].startswith(
        "imp: invalid YAML in config.yaml: ")
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out
    assert (home / "config.yaml").read_text() == broken
    assert not (tmp_path / "imp" / "config.yaml").exists()

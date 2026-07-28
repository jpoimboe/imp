"""`imp init` scaffolding: split committed imp/config.yaml + bootstrapped
global config (spec §4)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
import yaml

from imp.agents.interactive import InteractiveFailure, InteractiveSuccess
from imp.cli.init import (
    BaseDetection,
    InitScreenState,
    _screen_state_from_detection,
    run_init,
)
from imp.cli.init import _run_init_conversation as _real_run_init_conversation
from imp.cli.pythonpath_detect import ImportRoots
from imp.cli.verify_detect import DetectedTestCommand
from tests.conftest import _git, _init_repo


def _screen_state() -> InitScreenState:
    return _screen_state_from_detection(
        workflow="default",
        base=BaseDetection("master", "from origin/HEAD"),
        tests=DetectedTestCommand("make check", "from Makefile"),
        roots=ImportRoots(["src"], "from pyproject.toml"))


def _inputs(*answers: str) -> object:
    it = iter(answers)
    return lambda _prompt="": next(it)


def _picks(*selected: str) -> object:
    return lambda _available: list(selected)


def _conv_result(*, status="Success", git_init=False, default_workflow="default",
                 default_base="main", test_cmd_fast="", roots=()):
    return InteractiveSuccess({
        "status": status, "git_init": git_init,
        "default_workflow": default_workflow, "default_base": default_base,
        "test_cmd_fast": test_cmd_fast, "pythonpath": {"roots": list(roots)}})


def _stub_conv(monkeypatch, result) -> None:
    import imp.cli.init as initmod
    monkeypatch.setattr(initmod, "_run_init_conversation",
                        lambda **kw: result, raising=False)


def _stub_screen(monkeypatch, **overrides) -> None:
    """Accept the detected state on the confirmation screen, any row overridden."""
    from dataclasses import replace

    import imp.cli.init as initmod

    def _screen(_root, state, **_kw):
        return initmod.ScreenAccepted(replace(state, **overrides))

    monkeypatch.setattr(initmod, "_interactive_confirm_screen", _screen,
                        raising=False)


def _stub_screen_talk(monkeypatch) -> None:
    """Press `?` on the screen: hand the detected state to the conversation."""
    import imp.cli.init as initmod
    monkeypatch.setattr(
        initmod, "_interactive_confirm_screen",
        lambda _root, state, **_kw: initmod.ScreenTalk(state), raising=False)


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
    # Default them to "ready + accept as detected"; specific tests override. The
    # conversation is only reachable behind the screen's `?`.
    import imp.cli.init as initmod
    monkeypatch.setattr(initmod, "sandbox_available", lambda: True, raising=False)
    monkeypatch.setattr(initmod, "resolve_detection_provider",
                        lambda _providers, **_kw: ("anthropic", "m"), raising=False)
    monkeypatch.setattr(initmod, "_run_init_conversation",
                        lambda **kw: _conv_result(), raising=False)
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


def test_init_writes_optout_when_the_screen_opts_out(tmp_path, monkeypatch):
    _stub_screen(monkeypatch, test_cmd_fast="", tests_provenance="you set this")
    run_init(tmp_path, picker_fn=_picks("anthropic"), input_fn=_inputs(),
             output_fn=lambda _m: None)
    data = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    assert data["test_cmd_fast"] == ""


def test_init_comment_names_timeout_defaults(tmp_path, monkeypatch):
    _stub_screen(monkeypatch, test_cmd_fast="make check")
    run_init(tmp_path, picker_fn=_picks("anthropic"), input_fn=_inputs(),
             output_fn=lambda _m: None)
    text = (tmp_path / "imp" / "config.yaml").read_text()
    assert "120s" in text and "300s" in text
    assert "test_cmd_fast_timeout" in text
    assert "test_cmd_slow" in text and "test_cmd_slow_timeout" in text


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
    help_text = capsys.readouterr().out
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


def test_no_usable_credential_still_finishes_init(tmp_path, monkeypatch):
    # The hard-stop moved behind `?`: init needs no provider to write the config.
    import imp.cli.init as initmod
    monkeypatch.setattr(initmod, "resolve_detection_provider",
                        lambda _p, **_kw: None, raising=False)
    msgs: list[str] = []
    run_init(tmp_path, picker_fn=_picks("anthropic"), input_fn=_inputs(),
             output_fn=msgs.append)
    assert (tmp_path / "imp" / "config.yaml").is_file()
    assert "usable credential" not in "\n".join(msgs)
    user = tmp_path / "imphome" / "config.yaml"
    assert yaml.safe_load(user.read_text())["providers"] == ["anthropic"]


def test_no_usable_credential_behind_the_talk_key_keeps_the_screen(
    tmp_path, monkeypatch,
):
    import imp.cli.init as initmod
    monkeypatch.setattr(initmod, "resolve_detection_provider",
                        lambda _p, **_kw: None, raising=False)
    outcomes = iter(["talk", "accept"])

    def _screen(_root, state, **_kw):
        if next(outcomes) == "talk":
            return initmod.ScreenTalk(state)
        return initmod.ScreenAccepted(state)

    monkeypatch.setattr(initmod, "_interactive_confirm_screen", _screen,
                        raising=False)
    msgs: list[str] = []
    run_init(tmp_path, picker_fn=_picks("anthropic"), input_fn=_inputs(),
             output_fn=msgs.append)
    assert "imp needs a working model provider to talk this through." in msgs
    assert ("Set one up and re-run `imp init`, or fill the fields in yourself."
            in msgs)
    assert (tmp_path / "imp" / "config.yaml").is_file()   # returned to the screen


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


def test_conversation_writes_chosen_workflow_and_pythonpath(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    _stub_screen_talk(monkeypatch)
    _stub_conv(monkeypatch, _conv_result(
        default_workflow="minimal", default_base="develop",
        test_cmd_fast="make check", roots=["src"]))
    run_init(tmp_path, picker_fn=_picks("anthropic"), input_fn=_inputs(),
             output_fn=lambda _m: None)
    data = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    assert data["default-workflow"] == "minimal"      # agent's choice, not hardcoded
    assert data["default-base"] == "develop"
    assert data["test_cmd_fast"] == "make check"
    assert data["pythonpath"] == ["src"]


def test_conversation_pythonpath_validated_against_disk(tmp_path, monkeypatch):
    (tmp_path / "real").mkdir()
    _stub_screen_talk(monkeypatch)
    _stub_conv(monkeypatch, _conv_result(roots=["real", "ghost"]))
    run_init(tmp_path, picker_fn=_picks("anthropic"), input_fn=_inputs(),
             output_fn=lambda _m: None)
    data = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    assert data["pythonpath"] == ["real"]              # ghost dropped: not on disk


def test_conversation_empty_roots_writes_hint(tmp_path, monkeypatch):
    _stub_screen_talk(monkeypatch)
    _stub_conv(monkeypatch, _conv_result(roots=[]))
    run_init(tmp_path, picker_fn=_picks("anthropic"), input_fn=_inputs(),
             output_fn=lambda _m: None)
    text = (tmp_path / "imp" / "config.yaml").read_text()
    assert yaml.safe_load(text).get("pythonpath") is None
    assert "# pythonpath:" in text


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


def test_conversation_failure_stops_without_config_yaml(tmp_path, monkeypatch):
    _stub_screen_talk(monkeypatch)
    _stub_conv(monkeypatch, InteractiveFailure("auth", "bad_key"))
    msgs: list[str] = []
    run_init(tmp_path, picker_fn=_picks("anthropic"), input_fn=_inputs(),
             output_fn=msgs.append)
    assert not (tmp_path / "imp" / "config.yaml").exists()
    assert "re-run `imp init`" in "\n".join(msgs).lower()


def test_conversation_status_error_stops(tmp_path, monkeypatch):
    _stub_screen_talk(monkeypatch)
    _stub_conv(monkeypatch, _conv_result(status="Error"))
    msgs: list[str] = []
    run_init(tmp_path, picker_fn=_picks("anthropic"), input_fn=_inputs(),
             output_fn=msgs.append)
    assert not (tmp_path / "imp" / "config.yaml").exists()
    assert "didn't finish" in "\n".join(msgs).lower()


def test_write_init_files_renders_config_yaml_from_settings(tmp_path: Path) -> None:
    from imp.cli.init import InitSettings, write_init_files
    (tmp_path / "src").mkdir()
    settings = InitSettings(
        default_workflow="light",
        default_base="develop",
        test_cmd_fast="make check",
        pythonpath=["src"],
    )
    committed = write_init_files(tmp_path, settings)
    data = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    assert data["default-workflow"] == "light"
    assert data["default-base"] == "develop"
    assert data["vars"] == {}
    assert data["test_cmd_fast"] == "make check"
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
                     test_cmd_fast=None, pythonpath=[]),
    )
    text = (tmp_path / "imp" / "config.yaml").read_text()
    assert "test_cmd_fast" not in text          # None => block omitted entirely
    assert "# pythonpath:" in text              # empty roots => teaching hint
    assert ".imp/" in (tmp_path / ".gitignore").read_text()


def test_write_init_files_writes_optout_block_for_empty_string(tmp_path: Path) -> None:
    from imp.cli.init import InitSettings, write_init_files
    write_init_files(
        tmp_path,
        InitSettings(default_workflow="default", default_base="main",
                     test_cmd_fast="", pythonpath=[]),
    )
    data = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    assert data["test_cmd_fast"] == ""          # "" => explicit opt-out block written


def test_use_defaults_writes_config_yaml_no_model_call(tmp_path, monkeypatch):
    import imp.cli.init as initmod
    from imp.cli.init import run_init_noninteractive

    def _boom(*a, **k):
        raise AssertionError("--use-defaults must make no model/agent/provider call")

    monkeypatch.setattr(initmod, "resolve_detection_provider", _boom, raising=False)
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

    monkeypatch.setattr(m, "run_init_noninteractive", _spy)
    monkeypatch.setattr(m, "run_init",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("interactive path must not run without a tty")))
    args = m._build_parser().parse_args(["init", "--pythonpath", "src, ,lib"])
    args.func(args)
    assert called["project"] == tmp_path
    assert called["kw"]["pythonpath"] == ["src", "lib"]   # comma-split, blanks dropped


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
    monkeypatch.setattr(m, "run_init_noninteractive",
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
    monkeypatch.setattr(m, "run_init_noninteractive",
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
    monkeypatch.setattr(m, "run_init",
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
    monkeypatch.setattr(m, "run_init_noninteractive", lambda project, **kw: None)
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


def test_run_init_passes_the_setup_agents_declared_tier(tmp_path, monkeypatch):
    """run_init resolves the detection provider at the tier init.md declares."""
    import imp.cli.init as initmod
    from imp.assets import bundled_assets_dir
    from imp.engine.loader import parse_agent_file

    seen: dict[str, object] = {}

    def _capture(_providers, *, tier):
        seen["tier"] = tier
        return ("anthropic", "m")

    monkeypatch.setattr(initmod, "resolve_detection_provider", _capture,
                        raising=False)
    _stub_screen_talk(monkeypatch)
    run_init(project_root=tmp_path, picker_fn=_picks("anthropic"),
             input_fn=_inputs(), output_fn=lambda _msg: None)
    declared = parse_agent_file(bundled_assets_dir() / "init.md").tier
    assert seen["tier"] == declared


def test_run_init_declared_tier_follows_the_agent_file(tmp_path, monkeypatch):
    """The tier is read from init.md's frontmatter, not hardcoded: move the
    declaration and the tier run_init resolves at moves with it."""
    from dataclasses import replace

    import imp.cli.init as initmod

    real_parse = initmod.parse_agent_file

    def _declare_high(path):
        agent = real_parse(path)
        return replace(agent, tier="high") if path.name == "init.md" else agent

    seen: dict[str, object] = {}

    def _capture(_providers, *, tier):
        seen["tier"] = tier
        return ("anthropic", "m")

    monkeypatch.setattr(initmod, "parse_agent_file", _declare_high, raising=False)
    monkeypatch.setattr(initmod, "resolve_detection_provider", _capture,
                        raising=False)
    _stub_screen_talk(monkeypatch)
    run_init(project_root=tmp_path, picker_fn=_picks("anthropic"),
             input_fn=_inputs(), output_fn=lambda _msg: None)
    assert seen["tier"] == "high"


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
    from imp.providers.effective import global_config_path

    msgs: list[str] = []
    _colored_fresh_init(tmp_path, monkeypatch, msgs)
    user_path = global_config_path()

    # This one prints before the theme is applied, so its gold is the ansi gold.
    assert f"Setting up imp in {GOLD_ANSI}{tmp_path}{RESET}" in msgs
    assert f"{DIM}Enabled: anthropic{RESET}" in msgs
    assert f"{DIM}  anthropic → ANTHROPIC_API_KEY (set this env var){RESET}" in msgs
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
    from imp.providers.effective import global_config_path

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
    from imp.providers.effective import global_config_path

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
    # the screen draws and erases itself here; one blank, then the manifest
    assert msgs[i + 1] == ""
    assert "Created:" in msgs[i + 2]


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


def test_setup_conversation_prompt_uses_the_cancel_setup_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.init as initmod
    from imp.cli.prompt import SETUP_EMPTY_PROMPT_HINT

    seen: dict[str, object] = {}

    class _SpyPrompt:
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)

    def _fake_host(**kwargs: object) -> object:
        seen["handed_to_host"] = kwargs["prompt"]
        return _conv_result()

    monkeypatch.setattr(initmod, "_run_init_conversation", _real_run_init_conversation)
    monkeypatch.setattr(initmod, "Prompt", _SpyPrompt)
    monkeypatch.setattr(initmod, "run_interactive_agent_sync", _fake_host)

    initmod._run_init_conversation(
        project_root=tmp_path, state=_screen_state(),
        provider_name="anthropic", model_name="m",
        allow_unsandboxed=False, color=True, output_fn=lambda _m: None)

    assert seen["empty_hint"] == SETUP_EMPTY_PROMPT_HINT
    assert seen["empty_hint"] == "press ctrl-d to cancel setup"
    # The host gets the Prompt itself, not a bound read function: the shared
    # framing composes each read.
    assert isinstance(seen["handed_to_host"], _SpyPrompt)


def test_read_user_config_treats_malformed_yaml_as_empty(tmp_path: Path) -> None:
    # The clause must catch ruamel's YAMLError, not PyYAML's: init falls back to
    # asking rather than dying on a hand-broken machine config.
    from imp.cli.init import _read_user_config

    p = tmp_path / "config.yaml"
    p.write_text("providers: [anthropic\n")
    assert _read_user_config(p) == {}


def test_cmd_init_refuses_on_detached_head(tmp_path, monkeypatch, capsys):
    import imp.cli.main as m
    from imp.engine.worktree import checked_out_branch

    _init_repo(tmp_path)
    _git(tmp_path, "checkout", "-q", "--detach")
    head_before = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.setattr(m, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(m, "run_init_noninteractive",
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

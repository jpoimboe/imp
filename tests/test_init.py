"""`imp init` scaffolding: split committed imp/project.yaml + bootstrapped
user config (spec §4)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from imp.cli.init import run_init


def _inputs(*answers: str) -> object:
    it = iter(answers)
    return lambda _prompt="": next(it)


def _picks(*selected: str) -> object:
    return lambda _available: list(selected)


def _py_project(root: Path) -> None:
    (root / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    pkg = root / "src" / "x"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")


def _stub_gate(monkeypatch, *, provider=("anthropic", "m"), outcome=None):
    import imp.cli.init as initmod
    from imp.cli.pythonpath_detect import DetectedRoots
    monkeypatch.setattr(initmod, "sandbox_available", lambda: True, raising=False)
    monkeypatch.setattr(initmod, "resolve_detection_provider",
                        lambda _providers: provider, raising=False)
    monkeypatch.setattr(
        initmod, "detect_pythonpath_via_agent",
        lambda *a, **k: outcome if outcome is not None else DetectedRoots(["src"]),
        raising=False)


@pytest.fixture(autouse=True)
def _isolate_user_config(monkeypatch, tmp_path):
    # init bootstraps ~/.config/imp/config.yaml — pin XDG into the tmp tree so a
    # test NEVER writes the developer's real machine config.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


@pytest.fixture(autouse=True)
def _stub_theme_detection(monkeypatch):
    # init tests must NOT fire a real OSC-11 query at the terminal — pin the
    # detector to a deterministic 'ansi'. Persistence of a detected theme is
    # covered separately via the detect_theme_fn injection point.
    monkeypatch.setattr("imp.cli.init._default_detect_theme", lambda: "ansi")


@pytest.fixture(autouse=True)
def _git_repo(tmp_path, monkeypatch):
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")


def test_writes_project_yaml_workflows_gitignore(tmp_path: Path) -> None:
    run_init(
        project_root=tmp_path,
        picker_fn=_picks("anthropic"),
        input_fn=_inputs(),
        output_fn=lambda _msg: None,
    )
    proj = tmp_path / "imp" / "project.yaml"
    assert proj.is_file()
    data = yaml.safe_load(proj.read_text())
    # The committed project file carries the workflow ref + project settings —
    # NO bindings (the floor owns tiers), NO providers/theme (user-homed).
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
    user = tmp_path / "xdg" / "imp" / "config.yaml"
    assert user.is_file()
    data = yaml.safe_load(user.read_text())
    assert data["providers"] == ["anthropic"]
    assert data["editing_mode"] == "emacs"


def test_persists_detected_theme(tmp_path: Path) -> None:
    # The detected theme is written to the user config so the runtime never
    # re-queries the terminal.
    run_init(
        project_root=tmp_path, picker_fn=_picks("anthropic"),
        input_fn=_inputs(), output_fn=lambda _msg: None,
        detect_theme_fn=lambda: "dark",
    )
    data = yaml.safe_load((tmp_path / "xdg" / "imp" / "config.yaml").read_text())
    assert data["theme"] == "dark"


def test_user_config_documents_theme_and_editing_mode(tmp_path: Path) -> None:
    # safe_dump can't emit inline comments, so these are appended as raw commented lines.
    run_init(
        project_root=tmp_path, picker_fn=_picks("anthropic"),
        input_fn=_inputs(), output_fn=lambda _msg: None,
        detect_theme_fn=lambda: "dark",
    )
    text = (tmp_path / "xdg" / "imp" / "config.yaml").read_text()
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
    # init just bootstraps the machine's user config.
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    run_init(
        project_root=tmp_path, picker_fn=_picks("anthropic"),
        input_fn=_inputs(),
        output_fn=lambda _msg: None,
    )
    # Project file untouched; user config bootstrapped.
    assert (tmp_path / "imp" / "project.yaml").read_text() == "default-workflow: imp\n"
    assert (tmp_path / "xdg" / "imp" / "config.yaml").exists()


def test_supports_two_providers_recorded_in_user_config(tmp_path: Path) -> None:
    run_init(
        project_root=tmp_path,
        picker_fn=_picks("claude_code", "openai"),
        input_fn=_inputs(),
        output_fn=lambda _msg: None,
    )
    data = yaml.safe_load((tmp_path / "xdg" / "imp" / "config.yaml").read_text())
    assert data["providers"] == ["claude_code", "openai"]
    # Bindings are NOT scaffolded — the provider-code floor owns tiers.
    proj = yaml.safe_load((tmp_path / "imp" / "project.yaml").read_text())
    assert "tiers" not in proj


def test_supports_claude_code_no_env_prompt(tmp_path: Path) -> None:
    def _no_input(_prompt: str = "") -> str:
        raise AssertionError(f"unexpected input prompt: {_prompt!r}")

    run_init(
        project_root=tmp_path, picker_fn=_picks("claude_code"),
        input_fn=_no_input, output_fn=lambda _msg: None,
    )
    data = yaml.safe_load((tmp_path / "xdg" / "imp" / "config.yaml").read_text())
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


def test_init_no_verify_line_when_nothing_detected(tmp_path: Path) -> None:
    # A bare project (no Makefile/package.json/etc.) → no detection, no prompt,
    # no verify_command line (stays undecided; the `imp new` backstop covers it).
    run_init(
        tmp_path, input_fn=_inputs(), output_fn=lambda _m: None,
        picker_fn=lambda _avail: ["anthropic"],
    )
    text = (tmp_path / "imp" / "project.yaml").read_text()
    assert "test_cmd_fast" not in text
    assert "lint_command" not in text


def test_init_writes_test_cmd_fast_on_yes(tmp_path: Path) -> None:
    run_init(
        tmp_path, input_fn=_inputs("y"), output_fn=lambda _m: None,
        picker_fn=_picks("anthropic"),
        detect_verify_fn=lambda _root: "make check",
    )
    data = yaml.safe_load((tmp_path / "imp" / "project.yaml").read_text())
    assert data["test_cmd_fast"] == "make check"


def test_init_writes_optout_on_skip(tmp_path: Path) -> None:
    run_init(
        tmp_path, input_fn=_inputs("skip"), output_fn=lambda _m: None,
        picker_fn=_picks("anthropic"),
        detect_verify_fn=lambda _root: "make check",
    )
    data = yaml.safe_load((tmp_path / "imp" / "project.yaml").read_text())
    assert data["test_cmd_fast"] == ""


def test_init_comment_names_timeout_defaults(tmp_path: Path) -> None:
    run_init(
        tmp_path, input_fn=_inputs("y"), output_fn=lambda _m: None,
        picker_fn=_picks("anthropic"),
        detect_verify_fn=lambda _root: "make check",
    )
    text = (tmp_path / "imp" / "project.yaml").read_text()
    assert "120s" in text and "300s" in text
    assert "test_cmd_fast_timeout" in text
    assert "test_cmd_slow" in text
    assert "test_cmd_slow_timeout" in text


def test_init_surfaces_bwrap_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import imp.cli.init as initmod
    monkeypatch.setattr(initmod, "sandbox_available", lambda: False, raising=False)
    msgs: list[str] = []
    run_init(
        project_root=tmp_path,
        picker_fn=_picks("anthropic"),
        input_fn=_inputs(),
        output_fn=msgs.append,
        detect_theme_fn=lambda: "ansi",
    )
    blob = "\n".join(msgs)
    assert "bubblewrap" in blob.lower()


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


def test_init_scaffolds_commented_remote_driver(tmp_path: Path) -> None:
    run_init(
        project_root=tmp_path, picker_fn=_picks("anthropic"),
        input_fn=_inputs(), output_fn=lambda _msg: None,
    )
    text = (tmp_path / "xdg" / "imp" / "config.yaml").read_text()
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
    assert "Committed imp/project.yaml." in manifest
    assert "track in git" not in manifest


def test_already_initialized_with_existing_user_config_claims_no_write(
    tmp_path: Path,
) -> None:
    # Both the committed project file AND the machine user config already exist.
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    imp_config_dir = tmp_path / "xdg" / "imp"
    imp_config_dir.mkdir(parents=True)
    (imp_config_dir / "config.yaml").write_text("providers: [anthropic]\n")

    msgs: list[str] = []
    run_init(
        project_root=tmp_path, picker_fn=_picks("anthropic"),
        input_fn=_inputs(), output_fn=msgs.append,
    )
    text = "\n".join(msgs)
    # Must NOT claim a write that did not happen; user config is untouched.
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


def test_python_project_sets_pythonpath_and_informs(tmp_path, monkeypatch):
    from imp.cli.pythonpath_detect import DetectedRoots
    _py_project(tmp_path)
    _stub_gate(monkeypatch, outcome=DetectedRoots(["src"]))
    msgs: list[str] = []
    run_init(tmp_path, picker_fn=_picks("anthropic"), input_fn=_inputs(),
             output_fn=msgs.append, detect_verify_fn=lambda _r: None)
    data = yaml.safe_load((tmp_path / "imp" / "project.yaml").read_text())
    assert data["pythonpath"] == ["src"]
    blob = "\n".join(msgs)
    assert "Set pythonpath: ['src'] in imp/project.yaml." in blob
    assert "Review your configuration with `imp config`" in blob


def test_python_project_empty_roots_leaves_unset(tmp_path, monkeypatch):
    from imp.cli.pythonpath_detect import DetectedRoots
    _py_project(tmp_path)
    _stub_gate(monkeypatch, outcome=DetectedRoots([]))
    msgs: list[str] = []
    run_init(tmp_path, picker_fn=_picks("anthropic"), input_fn=_inputs(),
             output_fn=msgs.append, detect_verify_fn=lambda _r: None)
    text = (tmp_path / "imp" / "project.yaml").read_text()
    assert yaml.safe_load(text).get("pythonpath") is None
    assert "No installable Python package detected" in "\n".join(msgs)


def test_no_usable_credential_stops_without_project_yaml(tmp_path, monkeypatch):
    import imp.cli.init as initmod
    _py_project(tmp_path)
    monkeypatch.setattr(initmod, "sandbox_available", lambda: True, raising=False)
    monkeypatch.setattr(initmod, "resolve_detection_provider",
                        lambda _p: None, raising=False)
    msgs: list[str] = []
    run_init(tmp_path, picker_fn=_picks("anthropic"), input_fn=_inputs(),
             output_fn=msgs.append, detect_verify_fn=lambda _r: None)
    assert not (tmp_path / "imp" / "project.yaml").exists()
    assert "usable credential" in "\n".join(msgs)
    # Persisted so a re-run skips the picker.
    user = tmp_path / "xdg" / "imp" / "config.yaml"
    assert yaml.safe_load(user.read_text())["providers"] == ["anthropic"]


def test_configured_providers_skip_picker(tmp_path, monkeypatch):
    from imp.cli.pythonpath_detect import DetectedRoots
    _py_project(tmp_path)
    cfg_dir = tmp_path / "xdg" / "imp"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.yaml").write_text("providers: [anthropic]\ntheme: dark\n")
    _stub_gate(monkeypatch, outcome=DetectedRoots(["src"]))

    def _boom(_available):
        raise AssertionError("picker must not run when providers are configured")

    run_init(tmp_path, picker_fn=_boom, input_fn=_inputs(),
             output_fn=lambda _m: None, detect_verify_fn=lambda _r: None)
    assert yaml.safe_load(
        (tmp_path / "imp" / "project.yaml").read_text())["pythonpath"] == ["src"]


def test_bwrap_missing_offer_accept_continues(tmp_path, monkeypatch):
    import imp.cli.init as initmod
    from imp.cli.pythonpath_detect import DetectedRoots
    _py_project(tmp_path)
    monkeypatch.setattr(initmod, "sandbox_available", lambda: False, raising=False)
    monkeypatch.setattr(initmod, "resolve_detection_provider",
                        lambda _p: ("anthropic", "m"), raising=False)
    monkeypatch.setattr(initmod, "detect_pythonpath_via_agent",
                        lambda *a, **k: DetectedRoots(["src"]), raising=False)
    msgs: list[str] = []
    run_init(tmp_path, picker_fn=_picks("anthropic"), input_fn=_inputs("y"),
             output_fn=msgs.append, detect_verify_fn=lambda _r: None)
    assert yaml.safe_load(
        (tmp_path / "imp" / "project.yaml").read_text())["pythonpath"] == ["src"]
    assert "Running without an OS sandbox." in "\n".join(msgs)
    user = yaml.safe_load((tmp_path / "xdg" / "imp" / "config.yaml").read_text())
    assert user["sandbox"]["allow_unsandboxed"] is True


def test_bwrap_missing_offer_decline_stops(tmp_path, monkeypatch):
    import imp.cli.init as initmod
    _py_project(tmp_path)
    monkeypatch.setattr(initmod, "sandbox_available", lambda: False, raising=False)
    monkeypatch.setattr(initmod, "resolve_detection_provider",
                        lambda _p: ("anthropic", "m"), raising=False)
    msgs: list[str] = []
    run_init(tmp_path, picker_fn=_picks("anthropic"), input_fn=_inputs("n"),
             output_fn=msgs.append, detect_verify_fn=lambda _r: None)
    assert not (tmp_path / "imp" / "project.yaml").exists()
    assert "Install bubblewrap for a real filesystem boundary" in "\n".join(msgs)


def test_detection_credential_rejected_stops(tmp_path, monkeypatch):
    import imp.cli.init as initmod
    from imp.cli.pythonpath_detect import DetectionFailed
    _py_project(tmp_path)
    monkeypatch.setattr(initmod, "sandbox_available", lambda: True, raising=False)
    monkeypatch.setattr(initmod, "resolve_detection_provider",
                        lambda _p: ("claude_code", "m"), raising=False)
    monkeypatch.setattr(initmod, "detect_pythonpath_via_agent",
                        lambda *a, **k: DetectionFailed("credential", "rejected"),
                        raising=False)
    msgs: list[str] = []
    run_init(tmp_path, picker_fn=_picks("claude_code"), input_fn=_inputs(),
             output_fn=msgs.append, detect_verify_fn=lambda _r: None)
    assert not (tmp_path / "imp" / "project.yaml").exists()
    assert "the credential was rejected" in "\n".join(msgs)
    user = tmp_path / "xdg" / "imp" / "config.yaml"
    assert user.exists()


def test_detection_transient_failure_stops(tmp_path, monkeypatch):
    import imp.cli.init as initmod
    from imp.cli.pythonpath_detect import DetectionFailed
    _py_project(tmp_path)
    monkeypatch.setattr(initmod, "sandbox_available", lambda: True, raising=False)
    monkeypatch.setattr(initmod, "resolve_detection_provider",
                        lambda _p: ("anthropic", "m"), raising=False)
    monkeypatch.setattr(initmod, "detect_pythonpath_via_agent",
                        lambda *a, **k: DetectionFailed("transient", "timeout: slow"),
                        raising=False)
    msgs: list[str] = []
    run_init(tmp_path, picker_fn=_picks("anthropic"), input_fn=_inputs(),
             output_fn=msgs.append, detect_verify_fn=lambda _r: None)
    assert not (tmp_path / "imp" / "project.yaml").exists()
    assert "couldn't finish detecting" in "\n".join(msgs)
    user = tmp_path / "xdg" / "imp" / "config.yaml"
    assert user.exists()


def test_non_python_project_skips_detection_and_writes_hint(tmp_path, monkeypatch):
    import imp.cli.init as initmod

    def _boom(*a, **k):
        raise AssertionError("detection must not run for a non-Python project")

    monkeypatch.setattr(initmod, "detect_pythonpath_via_agent", _boom, raising=False)
    monkeypatch.setattr(initmod, "resolve_detection_provider", _boom, raising=False)
    run_init(tmp_path, picker_fn=_picks("anthropic"), input_fn=_inputs(),
             output_fn=lambda _m: None, detect_verify_fn=lambda _r: None)
    text = (tmp_path / "imp" / "project.yaml").read_text()
    assert yaml.safe_load(text).get("pythonpath") is None
    assert "# pythonpath:" in text


def test_write_init_files_renders_project_yaml_from_settings(tmp_path: Path) -> None:
    from imp.cli.init import InitSettings, write_init_files
    (tmp_path / "src").mkdir()
    settings = InitSettings(
        default_workflow="light",
        default_base="develop",
        test_cmd_fast="make check",
        pythonpath=["src"],
    )
    committed = write_init_files(tmp_path, settings)
    data = yaml.safe_load((tmp_path / "imp" / "project.yaml").read_text())
    assert data["default-workflow"] == "light"
    assert data["default-base"] == "develop"
    assert data["vars"] == {}
    assert data["test_cmd_fast"] == "make check"
    assert data["pythonpath"] == ["src"]
    # Memory scaffold written; commit created for a fresh tree.
    assert (tmp_path / "imp" / "memory" / "all.md").is_file()
    assert committed is True


def test_write_init_files_omits_test_block_when_none(tmp_path: Path) -> None:
    from imp.cli.init import InitSettings, write_init_files
    write_init_files(
        tmp_path,
        InitSettings(default_workflow="default", default_base="main",
                     test_cmd_fast=None, pythonpath=[]),
    )
    text = (tmp_path / "imp" / "project.yaml").read_text()
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
    data = yaml.safe_load((tmp_path / "imp" / "project.yaml").read_text())
    assert data["test_cmd_fast"] == ""          # "" => explicit opt-out block written


def test_use_defaults_writes_project_yaml_no_model_call(tmp_path, monkeypatch):
    import imp.cli.init as initmod
    from imp.cli.init import run_init_noninteractive

    def _boom(*a, **k):
        raise AssertionError("--use-defaults must make no model/agent/provider call")

    monkeypatch.setattr(initmod, "detect_pythonpath_via_agent", _boom, raising=False)
    monkeypatch.setattr(initmod, "resolve_detection_provider", _boom, raising=False)
    run_init_noninteractive(
        tmp_path, output_fn=lambda _m: None, detect_verify_fn=lambda _r: None)
    data = yaml.safe_load((tmp_path / "imp" / "project.yaml").read_text())
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
    data = yaml.safe_load((tmp_path / "imp" / "project.yaml").read_text())
    assert data["default-workflow"] == "light"
    assert data["default-base"] == "develop"
    assert data["pythonpath"] == ["pkg"]


def test_use_defaults_pythonpath_drops_absent_dirs(tmp_path):
    from imp.cli.init import run_init_noninteractive
    (tmp_path / "real").mkdir()
    run_init_noninteractive(
        tmp_path, output_fn=lambda _m: None,
        pythonpath=["real", "ghost"], detect_verify_fn=lambda _r: None)
    data = yaml.safe_load((tmp_path / "imp" / "project.yaml").read_text())
    assert data["pythonpath"] == ["real"]   # validated against disk


def test_use_defaults_fills_offline_test_command(tmp_path):
    from imp.cli.init import run_init_noninteractive
    run_init_noninteractive(
        tmp_path, output_fn=lambda _m: None,
        detect_verify_fn=lambda _r: "make check")   # heuristic hit, no prompt
    data = yaml.safe_load((tmp_path / "imp" / "project.yaml").read_text())
    assert data["test_cmd_fast"] == "make check"


def test_use_defaults_initializes_missing_git_repo(tmp_path_factory, monkeypatch):
    # A truly repo-less dir OUTSIDE the autouse _git_repo fixture's repo (which
    # git-inits tmp_path, so any tmp_path subdir is already inside a repo). Cap
    # git's upward repo discovery at the temp base so no ancestor repo is found;
    # user config stays isolated via the autouse _isolate_user_config fixture.
    from imp.cli.init import run_init_noninteractive
    fresh = tmp_path_factory.mktemp("fresh")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(fresh.parent))
    run_init_noninteractive(fresh, output_fn=lambda _m: None,
                            detect_verify_fn=lambda _r: None)
    assert (fresh / ".git").exists()
    assert (fresh / "imp" / "project.yaml").is_file()


def test_use_defaults_idempotent_on_existing_clone(tmp_path):
    from imp.cli.init import run_init_noninteractive
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    msgs: list[str] = []
    run_init_noninteractive(tmp_path, output_fn=msgs.append,
                            detect_verify_fn=lambda _r: None)
    assert (tmp_path / "imp" / "project.yaml").read_text() == "default-workflow: imp\n"
    assert "already initialized" in "\n".join(msgs).lower()


def test_use_defaults_idempotent_in_non_git_dir(tmp_path_factory, monkeypatch):
    # Re-init on a copied imp/project.yaml sitting in a dir with NO git repo must
    # not raise: the already-initialized branch ensures a repo before committing.
    from imp.cli.init import run_init_noninteractive
    fresh = tmp_path_factory.mktemp("copied")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(fresh.parent))
    (fresh / "imp").mkdir()
    (fresh / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    run_init_noninteractive(fresh, output_fn=lambda _m: None,
                            detect_verify_fn=lambda _r: None)
    assert (fresh / "imp" / "project.yaml").read_text() == "default-workflow: imp\n"
    assert (fresh / ".git").exists()


def test_use_defaults_raises_when_base_unresolvable(tmp_path):
    import subprocess

    from imp.cli.init import IntegrationBranchUnresolved, run_init_noninteractive

    # A born repo (has a commit, so not the unborn shortcut) with no origin
    # remote and no --base override: base resolution reaches the prompt seam,
    # which the non-interactive path wires to an EOF-raising stub, so it must
    # raise IntegrationBranchUnresolved rather than block on input.
    subprocess.run(["git", "commit", "--allow-empty", "-m", "seed", "-q"],
                   cwd=tmp_path, check=True)
    with pytest.raises(IntegrationBranchUnresolved):
        run_init_noninteractive(tmp_path, output_fn=lambda _m: None,
                                detect_verify_fn=lambda _r: None)

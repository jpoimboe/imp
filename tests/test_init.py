"""`imp init` scaffolding: split committed imp/config.yaml + bootstrapped
global config (spec §4)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
import yaml
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from imp.cli.init import run_init
from imp.paths import display_path
from imp.providers import ProviderStatus, Readiness
from tests._readiness import pin_readiness
from tests.conftest import _git, _init_repo


def _inputs(*answers: str) -> object:
    it = iter(answers)
    return lambda _prompt="": next(it)


def _picks(*selected: str) -> object:
    return lambda _available: list(selected)


_TWO_STATUSES = {
    "claude": ProviderStatus(Readiness.SIGNED_IN, "signed in"),
    "anthropic": ProviderStatus(Readiness.KEY_SET, "ANTHROPIC_API_KEY set"),
}


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


@pytest.fixture(autouse=True)
def _stub_theme_detection(monkeypatch):
    monkeypatch.setattr("imp.cli.init._default_detect_theme", lambda: "ansi")


@pytest.fixture(autouse=True)
def _wide_terminal(monkeypatch):
    # Host width varies (tty vs pipe); tests that exercise wrapping narrow it.
    monkeypatch.setenv("COLUMNS", "500")


@pytest.fixture(autouse=True)
def _pinned_readiness(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory,
) -> None:
    pin_readiness(monkeypatch, tmp_path_factory.mktemp("readiness"))


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
        picker_fn=_picks("claude", "openai"),
        input_fn=_inputs(),
        output_fn=lambda _msg: None,
    )
    data = yaml.safe_load((tmp_path / "imphome" / "config.yaml").read_text())
    assert data["providers"] == ["claude", "openai"]
    # Bindings are NOT scaffolded — the provider-code floor owns tiers.
    proj = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    assert "tiers" not in proj


def test_supports_claude_no_env_prompt(tmp_path: Path) -> None:
    def _no_input(_prompt: str = "") -> str:
        raise AssertionError(f"unexpected input prompt: {_prompt!r}")

    run_init(
        project_root=tmp_path, picker_fn=_picks("claude"),
        input_fn=_no_input, output_fn=lambda _msg: None,
    )
    data = yaml.safe_load((tmp_path / "imphome" / "config.yaml").read_text())
    assert data["providers"] == ["claude"]


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
    assert seen == [["claude", "anthropic", "openai", "codex"]]


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


def test_initial_selection_preselects_claude() -> None:
    from imp.cli.init import _initial_selection
    available = ["claude", "anthropic", "openai", "codex"]
    assert _initial_selection(available) == {0}


def test_initial_selection_empty_when_claude_absent() -> None:
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
    assert ("Committed imp/config.yaml, imp/memory/all.md, "
            "imp/memory/default/all.md, .gitignore.") in manifest
    assert "track in git" not in manifest


_HEAD_FILES = ("show", "--name-only", "--format=", "HEAD")


def test_fresh_init_commit_line_names_exactly_the_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.providers.paths import global_config_path

    _plain(monkeypatch)
    _stub_screen(monkeypatch, workflow="imp")
    msgs: list[str] = []
    run_init(tmp_path, picker_fn=_picks("anthropic"), input_fn=_inputs(),
             output_fn=msgs.append)

    assert ("Committed imp/config.yaml, imp/memory/all.md, "
            "imp/memory/imp/all.md, .gitignore.") in msgs
    created = msgs.index("Created:")
    assert msgs[created + 1:created + 4] == [
        "  imp/config.yaml", "  imp/memory/all.md", "  imp/memory/imp/all.md"]
    assert msgs[created + 4].startswith(f"  {display_path(global_config_path())}")
    assert sorted(_git(tmp_path, *_HEAD_FILES).stdout.split()) == sorted([
        "imp/config.yaml", "imp/memory/all.md", "imp/memory/imp/all.md",
        ".gitignore"])


def test_fresh_init_omits_an_already_committed_gitignore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plain(monkeypatch)
    (tmp_path / ".gitignore").write_text(".imp/\n")
    _git(tmp_path, "add", ".gitignore")
    _git(tmp_path, "commit", "-qm", "seed")
    _stub_screen(monkeypatch, workflow="imp")
    msgs: list[str] = []
    run_init(tmp_path, picker_fn=_picks("anthropic"), input_fn=_inputs(),
             output_fn=msgs.append)

    assert ("Committed imp/config.yaml, imp/memory/all.md, "
            "imp/memory/imp/all.md.") in msgs
    assert ".gitignore" not in _git(tmp_path, *_HEAD_FILES).stdout.split()


def test_reinit_names_only_the_gitignore_it_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plain(monkeypatch)
    (tmp_path / "imp" / "memory" / "imp").mkdir(parents=True)
    (tmp_path / "imp" / "config.yaml").write_text("default-workflow: imp\n")
    (tmp_path / "imp" / "memory" / "all.md").write_text("")
    (tmp_path / "imp" / "memory" / "imp" / "all.md").write_text("")
    _git(tmp_path, "add", "imp")
    _git(tmp_path, "commit", "-qm", "seed")

    msgs: list[str] = []
    run_init(tmp_path, picker_fn=_picks("anthropic"), input_fn=_inputs(),
             output_fn=msgs.append)
    assert [m for m in msgs if m.startswith("Committed")] == ["Committed .gitignore."]
    assert _git(tmp_path, *_HEAD_FILES).stdout.split() == [".gitignore"]

    again: list[str] = []
    run_init(tmp_path, picker_fn=_picks("anthropic"), input_fn=_inputs(),
             output_fn=again.append)
    assert not any(m.startswith("Committed") for m in again)


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

    rows = _picker_rows(["claude", "anthropic"], _TWO_STATUSES, cursor=0, selected={0})
    active = rows[1][1]
    inactive = rows[2][1]
    assert active.startswith("> ")
    assert "❯" not in active
    assert inactive.startswith("  ")


def test_init_finishes_with_no_credential_in_the_environment(tmp_path, monkeypatch):
    # init never probes provider credentials on any path — no probe, no message.
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
    assert (tmp_path / "imphome" / "config.yaml").is_file()


def test_fresh_init_writes_the_machine_config_only_after_accept(tmp_path, monkeypatch):
    import imp.cli.init as initmod
    monkeypatch.setattr(initmod, "sandbox_available", lambda: False, raising=False)
    user = tmp_path / "imphome" / "config.yaml"
    seen: dict[str, bool] = {}

    def _screen(_root, state, **_kw):
        seen["written_before_accept"] = user.exists()
        return initmod.ScreenAccepted(state)

    monkeypatch.setattr(initmod, "_interactive_confirm_screen", _screen, raising=False)
    run_init(tmp_path, picker_fn=_picks("claude", "openai"), input_fn=_inputs("y"),
             output_fn=lambda _m: None, detect_theme_fn=lambda: "dark")

    assert seen == {"written_before_accept": False}
    data = yaml.safe_load(user.read_text())
    assert data["providers"] == ["claude", "openai"]
    assert data["theme"] == "dark"
    assert data["editing_mode"] == "emacs"
    assert data["sandbox"] == {"allow_unsandboxed": True}


def test_bwrap_decline_still_repairs_an_invalid_provider_list(tmp_path, monkeypatch):
    import imp.cli.init as initmod
    _plain(monkeypatch)
    monkeypatch.setattr(initmod, "sandbox_available", lambda: False, raising=False)
    home = tmp_path / "imphome"
    home.mkdir(parents=True)
    before = _seed_machine_config(home, "providers: []\n")
    msgs: list[str] = []

    run_init(tmp_path, picker_fn=_picks("claude", "openai"), input_fn=_inputs("n"),
             output_fn=msgs.append)

    after = (home / "config.yaml").read_text()
    assert yaml.safe_load(after)["providers"] == ["claude", "openai"]
    assert _sans_providers(after) == _sans_providers(before)
    line = "Set providers in your machine config: claude, openai."
    install = next(i for i, m in enumerate(msgs)
                   if m.startswith("Install bubblewrap for a real filesystem boundary"))
    assert msgs.count(line) == 1
    assert msgs.index(line) < install
    assert not (tmp_path / "imp" / "config.yaml").exists()


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
    assert not (fresh / "imphome" / "config.yaml").exists()


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
    assert committed == (
        "imp/config.yaml", "imp/memory/all.md", "imp/memory/light/all.md",
        ".gitignore")
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
    assert ("--pythonpath: no such directory in this repo: srcc — leaving the "
            "Python import path unset.") in msgs


def test_written_config_comments_say_python_import_paths(tmp_path: Path) -> None:
    from imp.cli.init import run_init_noninteractive
    decided = tmp_path / "decided"
    (decided / "src").mkdir(parents=True)
    run_init_noninteractive(
        decided, output_fn=lambda _m: None,
        pythonpath=["src"], detect_verify_fn=lambda _r: None)
    decided_text = (decided / "imp" / "config.yaml").read_text()
    assert "# Worktree-relative Python import paths prepended to gate/agent\n" in decided_text

    undecided = tmp_path / "undecided"
    undecided.mkdir()
    run_init_noninteractive(
        undecided, output_fn=lambda _m: None, detect_verify_fn=lambda _r: None)
    undecided_text = (undecided / "imp" / "config.yaml").read_text()
    assert ("# pythonpath: worktree-relative Python import paths prepended to the "
            "gate/agent\n") in undecided_text

    for text in (decided_text, undecided_text):
        assert "import root" not in text


def test_init_help_documents_pythonpath_as_python_import_paths(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from imp.cli.main import _build_parser
    monkeypatch.setenv("COLUMNS", "200")
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["init", "-h"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert ("Comma-separated worktree-relative Python import paths recorded as "
            "pythonpath in the non-interactive path; validated against disk.") in help_text
    assert "import root" not in help_text


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
    # non-interactive path (no hang, no picker).
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
        "non_interactive": True, "workflow": None, "base": None,
        "pythonpath": None, "providers": None}
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


def test_cmd_init_ctrl_c_prints_cancelled_and_exits_user_ended(
    tmp_path, monkeypatch, capsys
):
    import imp.cli.main as m
    from imp.cli.exit_codes import ExitCode

    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(m, "_project_root", lambda: tmp_path)
    monkeypatch.setattr("imp.cli.init.run_init_noninteractive", interrupted)
    rc = m._cmd_init(_init_args())

    assert rc == ExitCode.USER_ENDED
    assert capsys.readouterr().err.splitlines() == ["imp: init cancelled"]


def test_main_init_cancelled_at_the_screen_writes_no_machine_config(
    tmp_path, monkeypatch, capsys,
):
    import imp.cli.init as initmod
    import imp.cli.main as m
    from imp.cli.exit_codes import ExitCode

    def _cancel(_root, _state, **_kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(initmod, "_interactive_confirm_screen", _cancel, raising=False)
    monkeypatch.setattr(m, "_select_headless", lambda _args: (False, False))
    monkeypatch.setattr("imp.diagnostics.install_stack_dumper", lambda _path: None)
    monkeypatch.chdir(tmp_path)

    rc = m.main(["init", "--providers", "claude"])

    assert rc == ExitCode.USER_ENDED
    assert capsys.readouterr().err.splitlines() == ["imp: init cancelled"]
    assert not (tmp_path / "imphome" / "config.yaml").exists()
    assert not (tmp_path / "imphome" / "projects").exists()
    assert not (tmp_path / "imp").exists()


def test_init_input_ends_the_line_when_cancelled(monkeypatch, capsys):
    import imp.cli.main as m

    def interrupted(prompt):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupted)
    with pytest.raises(KeyboardInterrupt):
        m._init_input("x: ")

    assert capsys.readouterr().err == "\n"


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


_AUTO_HEADLESS_NOTICE = (
    "No interactive terminal detected — writing defaults to imp/config.yaml. "
    "Override with --workflow / --base / --pythonpath / --providers.")


def _no_tty(monkeypatch) -> None:
    import imp.cli.main as m

    monkeypatch.setattr(m.sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(m.sys.stdout, "isatty", lambda: False, raising=False)


def test_cmd_init_auto_headless_fresh_prints_the_notice(tmp_path, monkeypatch, capsys):
    import imp.cli.main as m

    monkeypatch.setattr(m, "_project_root", lambda: tmp_path)
    _no_tty(monkeypatch)
    rc = m._cmd_init(_init_args(non_interactive=False))
    captured = capsys.readouterr()

    assert rc == 0
    assert _AUTO_HEADLESS_NOTICE in captured.err.splitlines()
    assert (tmp_path / "imp" / "config.yaml").is_file()


@pytest.mark.parametrize("non_interactive", [False, True])
def test_cmd_init_reinit_without_tty_omits_the_notice(
    tmp_path, monkeypatch, capsys, non_interactive
):
    import imp.cli.main as m

    monkeypatch.setattr(m, "_project_root", lambda: tmp_path)
    assert m._cmd_init(_init_args()) == 0  # --use-defaults: initialize first
    assert (tmp_path / "imp" / "config.yaml").is_file()
    capsys.readouterr()

    _no_tty(monkeypatch)
    rc = m._cmd_init(_init_args(non_interactive=non_interactive))
    captured = capsys.readouterr()

    assert rc == 0
    assert "No interactive terminal detected" not in captured.err
    assert ("Project already initialized (imp/config.yaml is committed)."
            in captured.out.splitlines())


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

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    msgs: list[str] = []
    _colored_fresh_init(tmp_path, monkeypatch, msgs)
    user_path = global_config_path()

    # This one prints before the theme is applied, so its gold is the ansi gold.
    assert f"Setting up imp in {GOLD_ANSI}{display_path(tmp_path)}{RESET}" in msgs
    assert f"{DIM}Enabled: anthropic{RESET}" in msgs
    assert f"{DIM}  anthropic — ANTHROPIC_API_KEY not set{RESET}" in msgs
    assert f"{DIM}Detected a dark terminal — colors tuned to match.{RESET}" in msgs
    assert f"{GOLD_DARK}Created:{RESET}" in msgs
    assert f"{DIM}  imp/config.yaml{RESET}" in msgs
    assert f"{DIM}  imp/memory/all.md{RESET}" in msgs
    assert f"{DIM}  imp/memory/default/all.md{RESET}" in msgs
    assert f"{DIM}  {display_path(user_path)}  (your machine: providers, theme){RESET}" in msgs
    assert (f"{DIM}Committed imp/config.yaml, imp/memory/all.md, "
            f"imp/memory/default/all.md, .gitignore.{RESET}") in msgs
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

    assert f"Setting up imp in {display_path(tmp_path)}" in msgs
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
    assert f"  {display_path(user_path)}  (your machine: providers, theme)" in msgs
    assert "Committed imp/config.yaml, .gitignore." in msgs


def test_noninteractive_output_stays_bare_under_forced_color(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.cli.init import run_init_noninteractive

    monkeypatch.setenv("FORCE_COLOR", "1")
    msgs: list[str] = []
    run_init_noninteractive(
        tmp_path, output_fn=msgs.append, detect_verify_fn=lambda _r: None)

    created = msgs.index("Created:")
    assert msgs[created + 1:] == [
        "  imp/config.yaml", "  imp/memory/all.md", "  imp/memory/default/all.md",
        "Committed imp/config.yaml, imp/memory/all.md, "
        "imp/memory/default/all.md, .gitignore."]
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

    rows = _picker_rows(["claude", "anthropic"], _TWO_STATUSES, cursor=0, selected={0})
    assert rows[1][0] == "ansiyellow bold"
    assert rows[2][0] == ""


def test_picker_docstring_no_longer_calls_the_cursor_row_cyan() -> None:
    from imp.cli.init import _interactive_provider_picker

    doc = _interactive_provider_picker.__doc__ or ""
    assert "ansicyan" not in doc
    assert "ansiyellow" in doc


_PICKER_HINT = "↑↓ move · space toggle · enter confirm · esc cancel"
_ORDER = ["claude", "anthropic", "openai", "codex"]


def _mockup_statuses(
    monkeypatch: pytest.MonkeyPatch, base: Path,
) -> dict[str, ProviderStatus]:
    """The spec mockup's machine: claude on PATH and signed in, codex absent, no keys."""
    from imp.providers import provider_readiness
    from imp.providers.effective import floor_config

    claude_dir, _ = pin_readiness(monkeypatch, base, on_path=("claude",), keys=())
    (claude_dir / ".credentials.json").write_text('{"claudeAiOauth": {}}')
    return {n: provider_readiness(n, floor_config()) for n in _ORDER}


def _fragment_lines(rows: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    lines: list[list[tuple[str, str]]] = [[]]
    for style, text in rows:
        for i, part in enumerate(text.split("\n")):
            if i:
                lines.append([])
            if part:
                lines[-1].append((style, part))
    return lines


def test_picker_rows_show_each_status_in_one_aligned_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.cli.init import _picker_rows

    statuses = _mockup_statuses(monkeypatch, tmp_path)
    rows = _picker_rows(_ORDER, statuses, cursor=0, selected={0})

    text = "".join(t for _style, t in rows).splitlines()
    assert text == [
        "Which providers do you want to enable?",
        "",
        "> [x] claude      signed in",
        "  [ ] anthropic   ANTHROPIC_API_KEY not set",
        "  [ ] openai      OPENAI_API_KEY not set",
        "  [ ] codex       CLI not found",
        "",
        _PICKER_HINT,
    ]
    labels = ["signed in", "ANTHROPIC_API_KEY not set", "OPENAI_API_KEY not set",
              "CLI not found"]
    assert len({line.index(label) for line, label in zip(text[2:6], labels, strict=True)}) == 1


def test_picker_label_is_dim_except_on_the_cursor_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.cli.init import _picker_rows

    statuses = _mockup_statuses(monkeypatch, tmp_path)
    rows = _picker_rows(_ORDER, statuses, cursor=1, selected={0})

    options = _fragment_lines(rows)[2:6]
    for i, (line, name) in enumerate(zip(options, _ORDER, strict=True)):
        label = statuses[name].label
        if i == 1:
            assert {style for style, _t in line} == {"ansiyellow bold"}
            assert "".join(t for _s, t in line).endswith(label)
        else:
            assert ("ansibrightblack", label) in line
            assert line[0][0] == ""


def test_provider_picker_erases_itself_on_confirm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import prompt_toolkit as pt

    from imp.cli.init import _interactive_provider_picker

    real_application = pt.Application
    captured: dict[str, object] = {}

    def spy(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return real_application(*args, **kwargs)

    monkeypatch.setattr(pt, "Application", spy)
    statuses = _mockup_statuses(monkeypatch, tmp_path)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("\r")             # confirm the pre-selected claude
        picked = _interactive_provider_picker(_ORDER, statuses)

    assert picked == ["claude"]
    assert captured.get("erase_when_done") is True


def _capture_default_picker(
    monkeypatch: pytest.MonkeyPatch, seen: dict[str, object], pick: list[str],
) -> None:
    def _picker(available: list[str], statuses: dict[str, ProviderStatus]) -> list[str]:
        seen["available"] = list(available)
        seen["labels"] = {n: s.label for n, s in statuses.items()}
        return pick

    monkeypatch.setattr("imp.cli.init._interactive_provider_picker", _picker)


def test_default_picker_gets_statuses_from_the_effective_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text(
        "default-workflow: imp\nprovider_envs: {anthropic: {env: MY_KEY}}\n")
    monkeypatch.delenv("MY_KEY", raising=False)
    seen: dict[str, object] = {}
    _capture_default_picker(monkeypatch, seen, ["anthropic"])

    run_init(
        project_root=tmp_path, settings_root=tmp_path,
        input_fn=_inputs(), output_fn=lambda _m: None,
    )

    assert seen["available"] == _ORDER
    assert seen["labels"] == {
        "claude": "signed in",
        "anthropic": "MY_KEY not set",
        "openai": "OPENAI_API_KEY set",
        "codex": "signed in",
    }


def test_reinit_picker_falls_back_to_the_floor_but_the_repaired_summary_keeps_provider_envs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # providers: [] fails to load until repaired; the picker falls back to the floor.
    _plain(monkeypatch)
    _mark_initialized(tmp_path)
    _write_machine_config("providers: []\nprovider_envs: {anthropic: {env: MY_KEY}}\n")
    monkeypatch.delenv("MY_KEY", raising=False)
    seen: dict[str, object] = {}
    _capture_default_picker(monkeypatch, seen, ["anthropic"])
    msgs: list[str] = []

    run_init(project_root=tmp_path, input_fn=_inputs(), output_fn=msgs.append)

    labels = seen["labels"]
    assert isinstance(labels, dict)
    assert labels["anthropic"] == "ANTHROPIC_API_KEY set"
    enabled = msgs.index("Enabled: anthropic")
    assert msgs[enabled + 1] == "  anthropic — MY_KEY not set"


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
    p.write_text("- claude\n- openai\n")
    with pytest.raises(ConfigWriteError) as exc:
        _read_user_config(p)
    # Same wording as imp's own config loader (src/imp/providers/config.py:123).
    assert str(exc.value) == "config.yaml root must be a mapping"


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


@pytest.mark.parametrize("initialized", [False, True])
def test_cmd_init_hands_run_init_the_imp_config_settings_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initialized: bool,
) -> None:
    import imp.cli.init as initmod
    import imp.cli.main as m

    if initialized:
        (tmp_path / "imp").mkdir()
        (tmp_path / "imp" / "config.yaml").write_text("default-workflow: default\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_select_headless", lambda _args: (False, False))
    seen: dict[str, object] = {}
    monkeypatch.setattr(initmod, "run_init", lambda _root, **kw: seen.update(kw))

    assert m._cmd_init(_init_args(non_interactive=False)) == 0
    assert seen["settings_root"] == (tmp_path.resolve() if initialized else None)


@pytest.mark.parametrize(("key", "expected"), [
    (None, ["  claude — not signed in · run `claude login`",
            "  anthropic — ANTHROPIC_API_KEY not set"]),
    ("sk-test", []),
])
def test_init_summary_lists_only_providers_that_need_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    key: str | None, expected: list[str],
) -> None:
    _plain(monkeypatch)
    if key is None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    msgs: list[str] = []

    run_init(
        project_root=tmp_path, picker_fn=_picks("claude", "anthropic"),
        input_fn=_inputs(), output_fn=msgs.append,
        detect_theme_fn=lambda: "ansi",
    )

    enabled = msgs.index("Enabled: claude, anthropic")
    assert msgs[enabled + 1:enabled + 1 + len(expected)] == expected
    assert [m for m in msgs if m.startswith(("  claude — ", "  anthropic — "))] == expected
    assert not any(m.endswith(" — ready") for m in msgs)
    assert not any("set this env var" in m for m in msgs)


def _plain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")


def _write_machine_config(text: str) -> None:
    from imp.providers.paths import global_config_path

    cfg = global_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(text)


def test_fresh_init_prints_no_line_for_a_signed_in_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plain(monkeypatch)
    msgs: list[str] = []

    run_init(
        project_root=tmp_path, picker_fn=_picks("claude"),
        input_fn=_inputs(), output_fn=msgs.append,
        detect_theme_fn=lambda: "ansi",
    )

    assert "Enabled: claude" in msgs
    assert not any(m.startswith("  claude — ") for m in msgs)
    assert not any("claude login" in m for m in msgs)


def test_init_summary_names_the_fix_for_each_not_ready_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    _plain(monkeypatch)
    pin_readiness(monkeypatch, tmp_path_factory.mktemp("not-ready"),
                  on_path=("claude",), keys=())
    msgs: list[str] = []

    run_init(
        project_root=tmp_path, picker_fn=_picks("claude", "codex"),
        input_fn=_inputs(), output_fn=msgs.append,
        detect_theme_fn=lambda: "ansi",
    )

    enabled = msgs.index("Enabled: claude, codex")
    assert msgs[enabled + 1:enabled + 3] == [
        "  claude — not signed in · run `claude login`",
        "  codex — CLI not found · install it, then run `codex login`",
    ]
    assert sum(m.startswith(("  claude — ", "  codex — ")) for m in msgs) == 2


def test_a_provider_outside_the_floor_is_enabled_without_a_status_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    _plain(monkeypatch)
    pin_readiness(monkeypatch, tmp_path_factory.mktemp("no-cli"), on_path=())
    msgs: list[str] = []

    run_init(
        project_root=tmp_path, input_fn=_inputs(), output_fn=msgs.append,
        providers_override=["housecat", "codex"],
    )

    enabled = msgs.index("Enabled: housecat, codex")
    assert msgs[enabled + 1] == "  codex — CLI not found · install it, then run `codex login`"
    assert not any(m.startswith("  housecat") for m in msgs)


_CLAUDE_CODEX_NOT_READY = [
    "  claude — not signed in · run `claude login`",
    "  codex — CLI not found · install it, then run `codex login`",
]


@pytest.mark.parametrize("source", ["picker", "flag", "machine config"])
def test_one_blank_line_then_the_summary_whatever_chose_the_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory, source: str,
) -> None:
    _plain(monkeypatch)
    pin_readiness(monkeypatch, tmp_path_factory.mktemp("summary"),
                  on_path=("claude",), keys=())
    if source == "machine config":
        _write_machine_config("providers: [claude, codex]\n")
    msgs: list[str] = []

    def _no_picker(_available: list[str]) -> list[str]:
        raise AssertionError("picker must not run")

    run_init(
        project_root=tmp_path,
        picker_fn=_picks("claude", "codex") if source == "picker" else _no_picker,
        providers_override=["claude", "codex"] if source == "flag" else None,
        input_fn=_inputs(), output_fn=msgs.append,
    )

    enabled = msgs.index("Enabled: claude, codex")
    header = msgs[:enabled - 1]
    assert header and header[0].startswith("Setting up imp in "), msgs
    assert "" not in header, msgs
    assert msgs[enabled - 1] == "", msgs
    assert msgs[enabled + 1:enabled + 3] == _CLAUDE_CODEX_NOT_READY
    assert not any(m.endswith(" — ready") for m in msgs)


def test_the_picker_opens_one_blank_line_below_the_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plain(monkeypatch)
    msgs: list[str] = []
    before_picker: list[str] = []

    def _pick(_available: list[str]) -> list[str]:
        before_picker.extend(msgs)
        return ["claude"]

    run_init(project_root=tmp_path, picker_fn=_pick, input_fn=_inputs(),
             output_fn=msgs.append)

    assert before_picker[0].startswith("Setting up imp in ")
    assert before_picker[-1] == ""
    assert "" not in before_picker[:-1]


_NOTE = "(your machine: providers, theme)"


def _mark_initialized(root: Path) -> None:
    (root / "imp").mkdir()
    (root / "imp" / "config.yaml").write_text("default-workflow: imp\n")


@pytest.mark.parametrize("already_initialized", [False, True])
@pytest.mark.parametrize("under_home", [True, False])
def test_init_shows_paths_under_home_with_a_tilde(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    under_home: bool, already_initialized: bool,
) -> None:
    _plain(monkeypatch)
    home = tmp_path.parent if under_home else tmp_path_factory.mktemp("elsewhere")
    monkeypatch.setenv("HOME", str(home))
    if already_initialized:
        _mark_initialized(tmp_path)
    root = f"~/{tmp_path.name}" if under_home else str(tmp_path)
    msgs: list[str] = []

    run_init(project_root=tmp_path, picker_fn=_picks("anthropic"),
             input_fn=_inputs(), output_fn=msgs.append)

    # The autouse fixture puts IMP_HOME at tmp_path/imphome.
    assert msgs[0] == f"Setting up imp in {root}", msgs
    assert f"  {root}/imphome/config.yaml  {_NOTE}" in msgs, msgs


@pytest.mark.parametrize("already_initialized", [False, True])
def test_a_machine_path_too_wide_for_its_note_puts_the_note_on_the_next_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, already_initialized: bool,
) -> None:
    from imp.cli import palette as pal

    _plain(monkeypatch)
    monkeypatch.setenv("COLUMNS", "100")
    home = tmp_path / "h"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("IMP_HOME", str(home / ("a" * 64)))
    if already_initialized:
        _mark_initialized(tmp_path)
    msgs: list[str] = []

    run_init(project_root=tmp_path, picker_fn=_picks("anthropic"),
             input_fn=_inputs(), output_fn=msgs.append)

    path_line = f"  ~/{'a' * 64}/config.yaml"
    assert len(path_line) <= 100 < len(f"{path_line}  {_NOTE}")
    at = msgs.index(path_line)
    assert msgs[at + 1] == f"    {_NOTE}", msgs
    assert all(pal.display_width(m) <= 100 for m in msgs[at:at + 2])


def test_a_header_too_wide_for_the_terminal_puts_the_path_on_its_own_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    from imp.cli import palette as pal

    _plain(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("elsewhere")))
    cols = len(f"Setting up imp in {tmp_path}") - 1
    monkeypatch.setenv("COLUMNS", str(cols))
    msgs: list[str] = []

    run_init(project_root=tmp_path, picker_fn=_picks("anthropic"),
             input_fn=_inputs(), output_fn=msgs.append)

    assert msgs[:3] == ["Setting up imp in", f"  {tmp_path}", ""], msgs
    assert all(pal.display_width(m) <= cols for m in msgs[:2])


def test_the_header_fit_ignores_color_escapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("IMP_THEME", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("elsewhere")))
    monkeypatch.setenv("COLUMNS", str(len(f"Setting up imp in {tmp_path}")))
    msgs: list[str] = []

    run_init(project_root=tmp_path, picker_fn=_picks("anthropic"),
             input_fn=_inputs(), output_fn=msgs.append)

    assert msgs[0] == f"Setting up imp in {GOLD_ANSI}{tmp_path}{RESET}", msgs


_MY_CLAUDE = "provider_envs: {claude: {env: MY_CLAUDE_KEY}}\n"
_MY_KEY = "provider_envs: {anthropic: {env: MY_KEY}}\n"


_CLAUDE_LOGIN = "  claude — not signed in · run `claude login`"
_CODEX_INSTALL = "  codex — CLI not found · install it, then run `codex login`"


# One case per readiness state init and `imp config` share; None means ready (no init line).
@pytest.mark.parametrize(("provider", "extra", "on_path", "env", "expected"), [
    ("claude", "", ("claude",), {}, None),
    ("codex", "", ("codex",), {}, None),
    ("claude", "", ("claude",), {"ANTHROPIC_API_KEY": None}, _CLAUDE_LOGIN),
    ("codex", "", (), {}, _CODEX_INSTALL),
    ("claude", _MY_CLAUDE, (), {"MY_CLAUDE_KEY": "k"}, None),
    ("claude", _MY_CLAUDE, (), {"MY_CLAUDE_KEY": None}, "  claude — MY_CLAUDE_KEY not set"),
    ("anthropic", "", (), {"ANTHROPIC_API_KEY": "k"}, None),
    ("anthropic", "", (), {"ANTHROPIC_API_KEY": None},
     "  anthropic — ANTHROPIC_API_KEY not set"),
    ("openai", "", (), {"OPENAI_API_KEY": None}, "  openai — OPENAI_API_KEY not set"),
    ("anthropic", _MY_KEY, (), {"MY_KEY": "k", "ANTHROPIC_API_KEY": None}, None),
    ("anthropic", _MY_KEY, (), {"MY_KEY": None, "ANTHROPIC_API_KEY": "k"},
     "  anthropic — MY_KEY not set"),
])
def test_init_readiness_verdict_matches_imp_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    provider: str, extra: str, on_path: tuple[str, ...],
    env: dict[str, str | None], expected: str | None,
) -> None:
    import io

    from imp.cli.config_cmd import render_config
    from imp.providers.effective import load_effective_config

    _plain(monkeypatch)
    pin_readiness(monkeypatch, tmp_path_factory.mktemp("parity"), on_path=on_path)
    for var, val in env.items():
        if val is None:
            monkeypatch.delenv(var, raising=False)
        else:
            monkeypatch.setenv(var, val)
    _write_machine_config(f"providers: [{provider}]\n{extra}")

    out = io.StringIO()
    render_config(load_effective_config(), out, state_dir=None, color=False)
    lines = out.getvalue().splitlines()
    rows = lines[lines.index("providers (local check):") + 1:]
    mark, _, label = next(r.split(maxsplit=2) for r in rows if r.split()[1:2] == [provider])

    msgs: list[str] = []
    run_init(
        project_root=tmp_path, picker_fn=_picks(provider),
        input_fn=_inputs(), output_fn=msgs.append,
    )

    status_lines = [m for m in msgs if m.startswith(f"  {provider} — ")]
    assert status_lines == ([] if expected is None else [expected])
    assert (expected is None) == (mark == "✓")
    if expected is not None:
        assert msgs[msgs.index(f"Enabled: {provider}") + 1] == expected
        assert expected.startswith(f"  {provider} — {label}")


# The bad value sits beside a valid providers: list so init reuses it; without one,
# init's provider repair rewrites the file and refuses the bad config before hints.
@pytest.mark.parametrize("key_set", [True, False])
def test_hint_step_degrades_to_floor_key_names_on_a_bad_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key_set: bool,
) -> None:
    _plain(monkeypatch)
    if key_set:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    else:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _write_machine_config(
        "providers: [claude, anthropic]\nprovider_envs: {anthropic: 5}\n")
    msgs: list[str] = []

    run_init(
        project_root=tmp_path, picker_fn=_picks("claude"),
        input_fn=_inputs(), output_fn=msgs.append,
    )

    enabled = msgs.index("Enabled: claude, anthropic")
    expected = [] if key_set else [
        "  claude — not signed in · run `claude login`",
        "  anthropic — ANTHROPIC_API_KEY not set",
    ]
    assert msgs[enabled + 1:enabled + 1 + len(expected)] == expected
    assert [m for m in msgs if m.startswith(("  claude — ", "  anthropic — "))] == expected
    assert ("Committed imp/config.yaml, imp/memory/all.md, "
            "imp/memory/default/all.md, .gitignore.") in msgs


def test_reinit_hints_honor_the_project_layer_provider_envs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plain(monkeypatch)
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text(
        "default-workflow: imp\nprovider_envs: {anthropic: {env: MY_KEY}}\n")
    _write_machine_config("providers: [anthropic]\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("MY_KEY", raising=False)
    msgs: list[str] = []

    run_init(
        project_root=tmp_path, settings_root=tmp_path, picker_fn=_picks("anthropic"),
        input_fn=_inputs(), output_fn=msgs.append,
    )

    enabled = msgs.index("Enabled: anthropic")
    assert msgs[enabled + 1] == "  anthropic — MY_KEY not set"
    assert "Project already initialized (imp/config.yaml is committed)." in msgs


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
            project_root=tmp_path, picker_fn=_picks("claude"),
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
        lambda *_args: (_ for _ in ()).throw(
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

    _set_user_providers(home / "config.yaml", ["claude", "openai"])

    after = (home / "config.yaml").read_text()
    assert "providers: [claude, openai]" in after
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

    _set_user_providers(home / "config.yaml", ["claude"])

    after = (home / "config.yaml").read_text()
    assert yaml.safe_load(after)["providers"] == ["claude"]
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
        _set_user_providers(home / "config.yaml", ["claude"])

    assert "would not read back as given" in str(exc.value)
    assert (home / "config.yaml").read_text() == before


@pytest.mark.parametrize(
    "raw",
    [
        None,                            # key absent
        [],                              # the state init used to write
        "claude",                   # hand-edited scalar: iterating yields chars
        ["claude", 7],              # _parse_providers rejects a non-string entry
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
    ["providers: []\n", "", "providers: claude\n",
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
        picker_fn=_picks("claude", "anthropic", "openai", "codex"),
        input_fn=_inputs(), output_fn=msgs.append,
    )

    after = (home / "config.yaml").read_text()
    assert yaml.safe_load(after)["providers"] == [
        "claude", "anthropic", "openai", "codex"]
    assert _sans_providers(after) == _sans_providers(before)
    assert ("Set providers in your machine config: "
            "claude, anthropic, openai, codex.") in msgs
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
        f"  {display_path(home / 'config.yaml')}  (your machine: providers, theme)")
    assert msgs[path_line + 1] == (
        "Change providers later: imp config set providers <name>,<name>")


def test_fresh_interactive_init_announces_provider_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh init (no imp/config.yaml) against a machine config with providers: []
    repairs it only after accept, announces it before Configured:, and preserves
    the existing comment."""
    import imp.cli.init as initmod

    _plain(monkeypatch)
    home = tmp_path / "imphome"
    home.mkdir(parents=True)
    before = _seed_machine_config(home, "providers: []\n")
    msgs: list[str] = []
    seen: dict[str, object] = {}

    def _screen(_root, state, **_kw):
        seen["msgs"] = list(msgs)
        seen["machine"] = (home / "config.yaml").read_text()
        return initmod.ScreenAccepted(state)

    monkeypatch.setattr(initmod, "_interactive_confirm_screen", _screen, raising=False)
    run_init(
        project_root=tmp_path,
        picker_fn=_picks("claude", "openai"),
        input_fn=_inputs(),
        output_fn=msgs.append,
    )

    line = "Set providers in your machine config: claude, openai."
    assert seen["machine"] == before
    before_screen = seen["msgs"]
    assert isinstance(before_screen, list)
    assert line not in before_screen
    after_screen = msgs[len(before_screen):]
    assert after_screen.index(line) < after_screen.index("Configured:")
    assert msgs.count(line) == 1
    after = (home / "config.yaml").read_text()
    assert yaml.safe_load(after)["providers"] == ["claude", "openai"]
    assert _sans_providers(after) == _sans_providers(before)


def test_fresh_init_cancel_leaves_an_invalid_machine_config_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.init as initmod

    home = tmp_path / "imphome"
    home.mkdir(parents=True)
    before = _seed_machine_config(home, "providers: []\n")

    def _cancel(_root, _state, **_kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(initmod, "_interactive_confirm_screen", _cancel, raising=False)
    msgs: list[str] = []
    with pytest.raises(KeyboardInterrupt):
        run_init(
            project_root=tmp_path, picker_fn=_picks("claude", "openai"),
            input_fn=_inputs(), output_fn=msgs.append,
        )

    assert (home / "config.yaml").read_bytes() == before.encode()
    assert not any("Set providers in your machine config:" in m for m in msgs)
    assert not (tmp_path / "imp" / "config.yaml").exists()


def test_fresh_interactive_init_no_repair_no_announcement(tmp_path: Path) -> None:
    """Fresh init (no imp/config.yaml) against a machine config with a valid
    provider list must NOT print the set-providers line (no repair happened)."""
    home = tmp_path / "imphome"
    home.mkdir(parents=True)
    _seed_machine_config(home, "providers: [anthropic]\n")

    def _boom(_available: list[str]) -> list[str]:
        raise AssertionError("picker must not run when providers are configured")

    msgs: list[str] = []
    run_init(
        project_root=tmp_path,
        picker_fn=_boom,
        input_fn=_inputs(),
        output_fn=msgs.append,
    )

    assert not any("Set providers in your machine config:" in m for m in msgs)


_DEFAULT_PROVIDERS = ["claude", "anthropic", "openai", "codex"]


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
        "Providers: claude, anthropic, openai, codex "
        "(the first authenticated one is used)")
    # imp's own readers must accept what init wrote — `providers: []` did not.
    assert list(load_effective_config(control_root=tmp_path).config.providers) == (
        _DEFAULT_PROVIDERS)
    monkeypatch.chdir(tmp_path)
    assert main(["config"]) == 0


@pytest.mark.parametrize(
    "seed",
    ["providers: []\n", "", "providers: claude\n",
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
        "claude, anthropic, openai, codex.",
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


def test_providers_flag_parses_a_preference_order() -> None:
    from imp.cli.init import parse_providers_flag

    assert parse_providers_flag("claude, openai") == ["claude", "openai"]
    # order is the user's, not the display order
    assert parse_providers_flag("codex,anthropic") == ["codex", "anthropic"]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("foo",
         "--providers: unknown provider 'foo' "
         "(choose from: claude, anthropic, openai, codex)"),
        # the `scripted` test backend is registered but not selectable here
        ("claude,scripted",
         "--providers: unknown provider 'scripted' "
         "(choose from: claude, anthropic, openai, codex)"),
        ("openai,openai", "--providers: duplicate provider 'openai'"),
        ("", "--providers: name at least one provider"),
        ("  ,  ", "--providers: name at least one provider"),
    ],
)
def test_providers_flag_refusals(raw: str, message: str) -> None:
    from imp.cli.init import InvalidProvidersFlag, parse_providers_flag

    with pytest.raises(InvalidProvidersFlag) as exc:
        parse_providers_flag(raw)
    assert str(exc.value) == message


def test_providers_override_skips_the_picker_on_a_fresh_init(tmp_path: Path) -> None:
    def _boom(_available: list[str]) -> list[str]:
        raise AssertionError("--providers must skip the picker")

    run_init(
        project_root=tmp_path, picker_fn=_boom, input_fn=_inputs(),
        output_fn=lambda _m: None,
        providers_override=["claude", "openai"],
    )

    user = tmp_path / "imphome" / "config.yaml"
    assert yaml.safe_load(user.read_text())["providers"] == ["claude", "openai"]


def test_providers_override_still_detects_the_theme(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skipping the picker must not skip theme detection: a fresh interactive
    init still renders the splash and screen in the terminal's own theme."""
    monkeypatch.delenv("IMP_THEME", raising=False)
    seen: dict[str, str] = {}
    _stub_screen_recording_theme(monkeypatch, seen)

    def _boom(_available: list[str]) -> list[str]:
        raise AssertionError("--providers must skip the picker")

    run_init(
        project_root=tmp_path, picker_fn=_boom, input_fn=_inputs(),
        output_fn=lambda _m: None, detect_theme_fn=lambda: "dark",
        providers_override=["openai"],
    )

    assert seen["theme"] == "dark"
    user = tmp_path / "imphome" / "config.yaml"
    assert yaml.safe_load(user.read_text())["theme"] == "dark"


def test_providers_override_rewrites_an_existing_valid_list(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text("default-workflow: imp\n")
    home = tmp_path / "imphome"
    home.mkdir(parents=True)
    before = _seed_machine_config(home, "providers: [anthropic]\n")

    def _boom(_available: list[str]) -> list[str]:
        raise AssertionError("--providers must skip the picker")

    msgs: list[str] = []
    run_init(
        project_root=tmp_path, picker_fn=_boom, input_fn=_inputs(),
        output_fn=msgs.append, providers_override=["codex", "openai"],
    )

    after = (home / "config.yaml").read_text()
    assert yaml.safe_load(after)["providers"] == ["codex", "openai"]
    assert _sans_providers(after) == _sans_providers(before)
    assert "Set providers in your machine config: codex, openai." in msgs
    assert not any("already set up" in m for m in msgs)
    # the list was just set explicitly — that line is for a reusing re-run
    assert not any(m.startswith("Change providers later:") for m in msgs)


def test_providers_override_bootstraps_absent_machine_config_for_initialized_project(
    tmp_path: Path,
) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text("default-workflow: imp\n")

    def _boom(_available: list[str]) -> list[str]:
        raise AssertionError("--providers must skip the picker")

    msgs: list[str] = []
    run_init(
        project_root=tmp_path, picker_fn=_boom, input_fn=_inputs(),
        output_fn=msgs.append, providers_override=["codex", "openai"],
    )

    user = tmp_path / "imphome" / "config.yaml"
    assert yaml.safe_load(user.read_text())["providers"] == ["codex", "openai"]
    assert "Bootstrapped your machine config (providers, theme)." in msgs


def test_use_defaults_providers_override_writes_the_given_list(
    tmp_path: Path,
) -> None:
    from imp.cli.init import run_init_noninteractive

    msgs: list[str] = []
    run_init_noninteractive(
        tmp_path, output_fn=msgs.append, detect_verify_fn=lambda _r: None,
        providers_override=["openai", "codex"])

    data = yaml.safe_load((tmp_path / "imphome" / "config.yaml").read_text())
    assert data["providers"] == ["openai", "codex"]
    branch_line = next(
        i for i, m in enumerate(msgs) if m.startswith("Integration branch: "))
    assert msgs[branch_line + 1] == (
        "Providers: openai, codex (the first authenticated one is used)")


def test_use_defaults_providers_override_rewrites_an_existing_valid_list(
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
        tmp_path, output_fn=msgs.append, detect_verify_fn=lambda _r: None,
        providers_override=["codex"])

    after = (home / "config.yaml").read_text()
    assert yaml.safe_load(after)["providers"] == ["codex"]
    assert _sans_providers(after) == _sans_providers(before)
    assert msgs == [
        "Project already initialized (imp/config.yaml is committed).",
        "Set providers in your machine config: codex.",
    ]


def test_init_parser_accepts_the_providers_flag() -> None:
    from imp.cli.main import _build_parser

    args = _build_parser().parse_args(["init", "--providers", "claude,openai"])
    assert args.providers == "claude,openai"
    assert _build_parser().parse_args(["init"]).providers is None


def test_cmd_init_passes_the_parsed_providers_to_both_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.main as m

    monkeypatch.setattr(m, "_project_root", lambda: tmp_path)
    called: dict[str, object] = {}
    monkeypatch.setattr("imp.cli.init.run_init_noninteractive",
                        lambda _project, **kw: called.update(kw))
    m._cmd_init(_init_args(providers="claude, openai"))
    assert called["providers_override"] == ["claude", "openai"]

    # Under pytest stdin is not a tty, so _select_headless would route the
    # second call headless too unless both streams are pinned open.
    called.clear()
    monkeypatch.setattr(m.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(m.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("imp.cli.init.run_init",
                        lambda _project, **kw: called.update(kw))
    m._cmd_init(_init_args(non_interactive=False, providers="codex"))
    assert called["providers_override"] == ["codex"]


@pytest.mark.parametrize(
    ("raw", "line"),
    [
        ("claude,foo",
         "imp: --providers: unknown provider 'foo' "
         "(choose from: claude, anthropic, openai, codex)"),
        ("openai,openai", "imp: --providers: duplicate provider 'openai'"),
        (",", "imp: --providers: name at least one provider"),
    ],
)
def test_cmd_init_rejects_a_bad_providers_value_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], raw: str, line: str,
) -> None:
    import imp.cli.main as m

    monkeypatch.setattr(m, "_project_root", lambda: tmp_path)
    for name in ("run_init", "run_init_noninteractive"):
        monkeypatch.setattr(
            f"imp.cli.init.{name}",
            lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("a bad --providers must not reach the writer")))

    rc = m._cmd_init(_init_args(providers=raw))

    captured = capsys.readouterr()
    assert rc == 2
    assert captured.err.splitlines() == [line]
    assert captured.out == ""
    assert not (tmp_path / "imp").exists()
    assert not (tmp_path / "imphome" / "config.yaml").exists()


def test_cmd_init_providers_flag_writes_exactly_that_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.main as m

    monkeypatch.setattr(m, "_project_root", lambda: tmp_path)

    rc = m._cmd_init(_init_args(providers="claude,openai"))

    assert rc == 0
    user = tmp_path / "imphome" / "config.yaml"
    assert yaml.safe_load(user.read_text())["providers"] == ["claude", "openai"]


def test_init_help_documents_providers_and_drops_provider_free(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from imp.cli.main import _build_parser

    with pytest.raises(SystemExit):
        _build_parser().parse_args(["init", "-h"])
    help_text = " ".join(capsys.readouterr().out.split())

    assert ("Comma-separated providers to enable, in preference order; skips "
            "the picker. Defaults to all four — the first authenticated one is "
            "used.") in help_text
    assert ("Non-interactive setup: write imp/config.yaml with the default "
            "workflow, resolved integration branch, and explanatory comments — "
            "no model call, works in CI with no credentials.") in help_text
    # init no longer skips providers on the non-interactive path
    assert "provider-free" not in help_text

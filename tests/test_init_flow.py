"""`imp init`'s detected-and-confirmed flow: the screen, and the `?` handoff."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from imp.agents.interactive import InteractiveFailure, InteractiveSuccess
from imp.cli.init import (
    InitScreenState,
    InitSettings,
    ScreenAccepted,
    ScreenTalk,
    _confirm_init_settings,
    _row_cells,
    _screen_state_from_detection,
    _settings_summary,
    run_init,
    write_init_files,
)
from imp.cli.pythonpath_detect import ImportRoots
from imp.cli.verify_detect import DetectedTestCommand


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")
    import imp.cli.init as initmod
    monkeypatch.setattr(initmod, "sandbox_available", lambda: True, raising=False)


def _repo_shaped_project(tmp_path: Path) -> Path:
    """A src-layout pyproject, a Makefile `check` target, and a git repo whose
    origin/HEAD resolves to master."""
    root = tmp_path / "proj"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "__init__.py").write_text("")
    (root / "pyproject.toml").write_text(
        '[tool.hatch.build.targets.wheel]\npackages = ["src/pkg"]\n')
    (root / "Makefile").write_text("check:\n\tpytest\n")
    subprocess.run(["git", "init", "-q", "-b", "master", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "symbolic-ref", "refs/remotes/origin/HEAD",
         "refs/remotes/origin/master"], check=True)
    return root


def _detected_state() -> InitScreenState:
    return _screen_state_from_detection(
        workflow="default",
        base=None,
        tests=DetectedTestCommand("make check", "from Makefile"),
        roots=ImportRoots(["src"], "from pyproject.toml"))


def _stub_screen(monkeypatch: pytest.MonkeyPatch, *outcomes: object) -> list[object]:
    """Hand back each screen outcome in turn — an outcome may be a `Screen*` value
    or the class itself, applied to the state the screen was given. Returns the
    list of states the screen saw, one per pass."""
    import imp.cli.init as initmod
    seen: list[object] = []
    queue = list(outcomes)

    def _screen(_root: Path, state: InitScreenState, **_kw: object) -> object:
        seen.append(state)
        outcome = queue.pop(0)
        return outcome(state) if callable(outcome) else outcome

    monkeypatch.setattr(initmod, "_interactive_confirm_screen", _screen,
                        raising=False)
    return seen


def _stub_conversation(
    monkeypatch: pytest.MonkeyPatch, result: object,
) -> dict[str, object]:
    """A credential-ready `?` path; returns the kwargs the conversation got."""
    import imp.cli.init as initmod
    captured: dict[str, object] = {}

    def _conv(**kw: object) -> object:
        captured.update(kw)
        return result

    monkeypatch.setattr(initmod, "resolve_detection_provider",
                        lambda _p, **_kw: ("anthropic", "m"), raising=False)
    monkeypatch.setattr(initmod, "_run_init_conversation", _conv, raising=False)
    return captured


def _conv_result(**over: object) -> InteractiveSuccess:
    payload = {"status": "Success", "git_init": False, "default_workflow": "default",
               "default_base": "main", "test_cmd_fast": "", "pythonpath": {"roots": []}}
    return InteractiveSuccess({**payload, **over})


def test_accepting_the_screen_writes_the_rows_as_they_stand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _screen_state_from_detection(
        workflow="minimal", base=None, tests=None,
        roots=ImportRoots([], "not a Python project"))
    _stub_screen(monkeypatch, ScreenAccepted(state))
    settings = _confirm_init_settings(
        tmp_path, state, providers=["anthropic"], allow_unsandboxed=False,
        color=False, output_fn=lambda _m: None)
    assert settings is not None
    assert settings.default_workflow == "minimal"
    # None omits the block + writes the teaching hint; "" is the user's opt-out.
    assert settings.test_cmd_fast is None
    assert settings.pythonpath == []


def test_accepting_an_edited_empty_tests_row_writes_the_explicit_opt_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = replace(_detected_state(), test_cmd_fast="",
                    tests_provenance="you set this")
    _stub_screen(monkeypatch, ScreenAccepted(state))
    settings = _confirm_init_settings(
        tmp_path, state, providers=["anthropic"], allow_unsandboxed=False,
        color=False, output_fn=lambda _m: None)
    assert settings is not None and settings.test_cmd_fast == ""


def test_talk_seeds_the_conversation_with_the_screens_current_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    edited = replace(_detected_state(), branch="trunk",
                     branch_provenance="you set this")
    _stub_screen(monkeypatch, ScreenTalk(edited))
    captured = _stub_conversation(monkeypatch, _conv_result())
    _confirm_init_settings(
        tmp_path, _detected_state(), providers=["anthropic"], allow_unsandboxed=False,
        color=False, output_fn=lambda _m: None)
    handed = captured["state"]
    assert isinstance(handed, InitScreenState)
    assert handed.branch == "trunk"
    assert handed.branch_provenance == "you set this"


def test_talk_writes_the_conversations_values_not_the_screens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "real").mkdir()
    _stub_screen(monkeypatch, ScreenTalk(_detected_state()))
    _stub_conversation(monkeypatch, _conv_result(
        default_workflow="minimal", default_base="develop",
        test_cmd_fast="just check", pythonpath={"roots": ["real", "ghost"]}))
    settings = _confirm_init_settings(
        tmp_path, _detected_state(), providers=["anthropic"],
        allow_unsandboxed=False, color=False, output_fn=lambda _m: None)
    assert settings is not None
    assert settings.default_workflow == "minimal"
    assert settings.default_base == "develop"
    assert settings.test_cmd_fast == "just check"
    assert settings.pythonpath == ["real"]      # ghost dropped: not on disk


def test_a_conversation_omitting_the_test_command_leaves_the_key_unwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An omitted field is the agent saying "not yet", not the opt-out: the key
    stays absent so imp can raise the gate again."""
    payload = dict(_conv_result().output)
    del payload["test_cmd_fast"]
    _stub_screen(monkeypatch, ScreenTalk(_detected_state()))
    _stub_conversation(monkeypatch, InteractiveSuccess(payload))
    settings = _confirm_init_settings(
        tmp_path, _detected_state(), providers=["anthropic"],
        allow_unsandboxed=False, color=False, output_fn=lambda _m: None)
    assert settings is not None
    assert settings.test_cmd_fast is None
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    write_init_files(tmp_path, settings)
    assert "test_cmd_fast" not in (tmp_path / "imp" / "config.yaml").read_text()


def test_a_conversation_returning_an_empty_test_command_writes_the_opt_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty string is still the deliberate opt-out, written as an explicit key."""
    _stub_screen(monkeypatch, ScreenTalk(_detected_state()))
    _stub_conversation(monkeypatch, _conv_result(test_cmd_fast=""))
    settings = _confirm_init_settings(
        tmp_path, _detected_state(), providers=["anthropic"],
        allow_unsandboxed=False, color=False, output_fn=lambda _m: None)
    assert settings is not None
    assert settings.test_cmd_fast == ""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    write_init_files(tmp_path, settings)
    data = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    assert data["test_cmd_fast"] == ""


def test_talk_without_a_credential_prints_the_relocated_message_and_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.init as initmod
    monkeypatch.setattr(initmod, "resolve_detection_provider",
                        lambda _p, **_kw: None, raising=False)
    monkeypatch.setattr(
        initmod, "_run_init_conversation",
        lambda **_kw: pytest.fail("no credential must not reach the conversation"),
        raising=False)
    seen = _stub_screen(monkeypatch, ScreenTalk(_detected_state()), ScreenAccepted)
    msgs: list[str] = []
    settings = _confirm_init_settings(
        tmp_path, _detected_state(), providers=["anthropic"],
        allow_unsandboxed=False, color=False, output_fn=msgs.append)
    assert msgs == [
        "imp needs a working model provider to talk this through.",
        "None of the providers you enabled has a usable credential yet:",
        "  anthropic → set ANTHROPIC_API_KEY in your environment",
        "Set one up and re-run `imp init`, or fill the fields in yourself.",
    ]
    assert len(seen) == 2                 # returned to the screen, init not aborted
    assert settings is not None and settings.test_cmd_fast == "make check"


def test_a_failed_conversation_stops_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_screen(monkeypatch, ScreenTalk(_detected_state()))
    _stub_conversation(monkeypatch, InteractiveFailure("auth", "bad_key"))
    msgs: list[str] = []
    settings = _confirm_init_settings(
        tmp_path, _detected_state(), providers=["anthropic"],
        allow_unsandboxed=False, color=False, output_fn=msgs.append)
    assert settings is None
    assert "re-run `imp init`" in "\n".join(msgs).lower()


def test_a_non_success_conversation_stops_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_screen(monkeypatch, ScreenTalk(_detected_state()))
    _stub_conversation(monkeypatch, _conv_result(status="Error"))
    msgs: list[str] = []
    settings = _confirm_init_settings(
        tmp_path, _detected_state(), providers=["anthropic"],
        allow_unsandboxed=False, color=False, output_fn=msgs.append)
    assert settings is None
    assert "didn't finish" in "\n".join(msgs).lower()


def test_fresh_init_reaches_the_screen_with_no_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.init as initmod
    root = _repo_shaped_project(tmp_path)
    monkeypatch.setattr(
        initmod, "resolve_detection_provider",
        lambda *_a, **_kw: pytest.fail("init must not resolve a provider"),
        raising=False)
    seen = _stub_screen(monkeypatch, ScreenAccepted)
    run_init(root, picker_fn=lambda _a: ["anthropic"], input_fn=lambda _p: "",
             output_fn=lambda _m: None, detect_theme_fn=lambda: "ansi")
    state = seen[0]
    assert isinstance(state, InitScreenState)
    assert [(value, note) for _label, value, note in _row_cells(state).values()] == [
        ("default", "full pipeline — spec, plan, implement, review, merge"),
        ("master", "from origin/HEAD"),
        ("make check", "from Makefile"),
        ("src", "from pyproject.toml"),
    ]


def test_one_enter_writes_and_commits_the_detected_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo_shaped_project(tmp_path)
    _stub_screen(monkeypatch, ScreenAccepted)
    run_init(root, picker_fn=lambda _a: ["anthropic"], input_fn=lambda _p: "",
             output_fn=lambda _m: None, detect_theme_fn=lambda: "ansi")
    data = yaml.safe_load((root / "imp" / "config.yaml").read_text())
    assert data["default-workflow"] == "default"
    assert data["default-base"] == "master"
    assert data["test_cmd_fast"] == "make check"
    assert data["pythonpath"] == ["src"]
    subject = subprocess.run(
        ["git", "-C", str(root), "log", "-1", "--format=%s"],
        capture_output=True, text=True, check=True).stdout.strip()
    assert subject == "imp: initialize project"


def test_one_enter_echoes_configured_block_above_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo_shaped_project(tmp_path)
    _stub_screen(monkeypatch, ScreenAccepted)
    msgs: list[str] = []
    run_init(root, picker_fn=lambda _a: ["anthropic"], input_fn=lambda _p: "",
             output_fn=msgs.append, detect_theme_fn=lambda: "ansi")
    configured_idx = next(i for i, m in enumerate(msgs) if "Configured:" in m)
    created_idx = next(i for i, m in enumerate(msgs) if "Created:" in m)
    assert configured_idx < created_idx
    assert "workflow" in msgs[configured_idx + 1] and "default" in msgs[configured_idx + 1]
    assert "branch" in msgs[configured_idx + 2] and "master" in msgs[configured_idx + 2]
    assert "tests" in msgs[configured_idx + 3] and "make check" in msgs[configured_idx + 3]
    assert "import root" in msgs[configured_idx + 4] and "src" in msgs[configured_idx + 4]


def test_settings_summary_empty_values_render_as_none() -> None:
    settings = InitSettings(
        default_workflow="default", default_base="main",
        test_cmd_fast=None, pythonpath=[])
    lines = _settings_summary(settings)
    assert len(lines) == 4
    tests_line = next(line for line in lines if "tests" in line)
    roots_line = next(line for line in lines if "import root" in line)
    assert "(none)" in tests_line
    assert "(none)" in roots_line

    settings_empty_base = InitSettings(
        default_workflow="default", default_base="",
        test_cmd_fast=None, pythonpath=[])
    lines_empty_base = _settings_summary(settings_empty_base)
    branch_line = next(line for line in lines_empty_base if "branch" in line)
    assert "(none)" in branch_line


def test_init_finishes_with_no_provider_credential_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.init as initmod
    root = _repo_shaped_project(tmp_path)
    monkeypatch.setattr(initmod, "resolve_detection_provider",
                        lambda _p, **_kw: None, raising=False)
    _stub_screen(monkeypatch, ScreenAccepted)
    msgs: list[str] = []
    run_init(root, picker_fn=lambda _a: ["anthropic"], input_fn=lambda _p: "",
             output_fn=msgs.append, detect_theme_fn=lambda: "ansi")
    assert (root / "imp" / "config.yaml").is_file()
    assert "working model provider" not in "\n".join(msgs)


def test_the_git_question_is_asked_verbatim_and_bare_enter_accepts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "norepo"
    root.mkdir()
    _stub_screen(monkeypatch, ScreenAccepted)
    asked: list[str] = []

    def _ask(prompt: str) -> str:
        asked.append(prompt)
        return ""

    run_init(root, picker_fn=lambda _a: ["anthropic"], input_fn=_ask,
             output_fn=lambda _m: None, detect_theme_fn=lambda: "ansi")
    assert asked == ["imp needs a git repository. Initialize one here? [Y/n] "]
    assert (root / ".git").exists()
    assert (root / "imp" / "config.yaml").is_file()


def test_declining_git_writes_nothing_and_says_so_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.init as initmod
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "norepo"
    root.mkdir()
    monkeypatch.setattr(
        initmod, "_interactive_confirm_screen",
        lambda *_a, **_kw: pytest.fail("a declined repo must not reach the screen"),
        raising=False)
    msgs: list[str] = []
    run_init(root, picker_fn=lambda _a: ["anthropic"], input_fn=lambda _p: "n",
             output_fn=msgs.append, detect_theme_fn=lambda: "ansi")
    assert msgs[-1] == (
        "imp needs a git repository to track quests. "
        "Re-run `imp init` when you're ready.")
    assert not (root / ".git").exists()
    assert not (root / "imp").exists()


def test_cancelling_the_screen_raises_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.init as initmod
    root = _repo_shaped_project(tmp_path)

    def _cancel(*_a: object, **_kw: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(initmod, "_interactive_confirm_screen", _cancel,
                        raising=False)
    with pytest.raises(KeyboardInterrupt):
        run_init(root, picker_fn=lambda _a: ["anthropic"], input_fn=lambda _p: "",
                 output_fn=lambda _m: None, detect_theme_fn=lambda: "ansi")
    assert not (root / "imp").exists()


def test_an_untouched_nothing_detected_tests_row_writes_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "plain"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    _stub_screen(monkeypatch, ScreenAccepted)
    run_init(root, picker_fn=lambda _a: ["anthropic"], input_fn=lambda _p: "",
             output_fn=lambda _m: None, detect_theme_fn=lambda: "ansi")
    text = (root / "imp" / "config.yaml").read_text()
    assert "test_cmd_fast" not in text          # None omits the block entirely
    assert yaml.safe_load(text)["default-base"] == "main"

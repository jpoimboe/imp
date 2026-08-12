"""`imp init`'s detected-and-confirmed flow: the screen, and what accepting it writes."""

from __future__ import annotations

import importlib.util
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from imp.cli.init import (
    InitScreenState,
    InitSettings,
    ScreenAccepted,
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
        setup=None,
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


def test_accepting_the_screen_writes_the_rows_as_they_stand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _screen_state_from_detection(
        workflow="minimal", base=None, tests=None, setup=None,
        roots=ImportRoots([], "not a Python project"))
    _stub_screen(monkeypatch, ScreenAccepted(state))
    settings = _confirm_init_settings(tmp_path, state)
    assert settings is not None
    assert settings.default_workflow == "minimal"
    # None never decided; False is the user's "this project needs none".
    assert settings.test_cmd_fast is None
    assert settings.worktree_setup is None
    assert settings.pythonpath is None


def test_accepting_an_edited_empty_tests_row_writes_needs_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = replace(_detected_state(), test_cmd_fast=False,
                    tests_provenance="you set this")
    _stub_screen(monkeypatch, ScreenAccepted(state))
    settings = _confirm_init_settings(tmp_path, state)
    assert settings is not None and settings.test_cmd_fast is False


def test_fresh_init_reaches_the_screen_with_no_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo_shaped_project(tmp_path)
    monkeypatch.setattr(
        "imp.providers.resolve.available_providers",
        lambda *_a, **_kw: pytest.fail("init must not resolve a provider"))
    seen = _stub_screen(monkeypatch, ScreenAccepted)
    run_init(root, picker_fn=lambda _a: ["anthropic"], input_fn=lambda _p: "",
             output_fn=lambda _m: None, detect_theme_fn=lambda: "ansi")
    state = seen[0]
    assert isinstance(state, InitScreenState)
    assert [(value, note) for _label, value, note in _row_cells(state).values()] == [
        ("default", "full pipeline — spec, plan, implement, review, merge"),
        ("master", "from origin/HEAD"),
        ("make check", "from Makefile"),
        ("(none)", "nothing detected — no command runs in new worktrees"),
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
    assert ("worktree setup" in msgs[configured_idx + 4]
            and "(none)" in msgs[configured_idx + 4])
    assert "import root" in msgs[configured_idx + 5] and "src" in msgs[configured_idx + 5]


def test_settings_summary_empty_values_render_as_none() -> None:
    settings = InitSettings(
        default_workflow="default", default_base="main",
        test_cmd_fast=None, worktree_setup=None, pythonpath=None)
    lines = _settings_summary(settings)
    assert len(lines) == 5
    tests_line = next(line for line in lines if "tests" in line)
    setup_line = next(line for line in lines if "worktree setup" in line)
    roots_line = next(line for line in lines if "import root" in line)
    assert "(none)" in tests_line
    assert "(none)" in setup_line
    assert "(none)" in roots_line

    settings_empty_base = InitSettings(
        default_workflow="default", default_base="",
        test_cmd_fast=None, worktree_setup=None, pythonpath=None)
    lines_empty_base = _settings_summary(settings_empty_base)
    branch_line = next(line for line in lines_empty_base if "branch" in line)
    assert "(none)" in branch_line

    decided_none = InitSettings(
        default_workflow="default", default_base="main",
        test_cmd_fast=False, worktree_setup=False, pythonpath=False)
    lines_false = _settings_summary(decided_none)
    assert "(none needed)" in next(ln for ln in lines_false if "tests" in ln)
    assert "(none needed)" in next(ln for ln in lines_false if "worktree setup" in ln)
    assert "(none needed)" in next(ln for ln in lines_false if "import root" in ln)


def test_init_finishes_with_no_provider_credential_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo_shaped_project(tmp_path)
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
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


@pytest.mark.parametrize("test_cmd_fast", [None, "make check", False])
@pytest.mark.parametrize("worktree_setup", [None, "npm ci", False])
@pytest.mark.parametrize("pythonpath", [None, ["src"], False])
def test_every_config_an_init_run_can_write_is_loadable(
    tmp_path: Path, test_cmd_fast: object, worktree_setup: object, pythonpath: object,
) -> None:
    from imp.providers.config import parse_config_layer

    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    write_init_files(root, InitSettings(
        default_workflow="default", default_base="main",
        test_cmd_fast=test_cmd_fast, worktree_setup=worktree_setup,
        pythonpath=pythonpath))   # type: ignore[arg-type]
    layer, _src = parse_config_layer(root / "imp" / "config.yaml")  # type: ignore[misc]
    if test_cmd_fast is None:
        assert "test_cmd_fast" not in layer
    else:
        assert layer["test_cmd_fast"] == test_cmd_fast
    if worktree_setup is None:
        assert "worktree_setup" not in layer
    else:
        assert layer["worktree_setup"] == worktree_setup
    if pythonpath is None:
        assert "pythonpath" not in layer
        assert "# pythonpath: [src]" in (root / "imp" / "config.yaml").read_text()
    else:
        assert layer["pythonpath"] == pythonpath


def test_run_init_seeds_the_screen_with_the_detected_worktree_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo_shaped_project(tmp_path)
    (root / "uv.lock").write_text("")
    seen = _stub_screen(monkeypatch, ScreenAccepted)
    run_init(root, picker_fn=lambda _a: ["anthropic"], input_fn=lambda _p: "",
             output_fn=lambda _m: None, detect_theme_fn=lambda: "ansi")
    state = seen[0]
    assert state.worktree_setup == "uv sync --all-extras"
    assert state.setup_provenance == "from uv.lock"
    assert yaml.safe_load(
        (root / "imp" / "config.yaml").read_text())["worktree_setup"] == (
            "uv sync --all-extras")


def test_the_talk_it_through_path_is_retired() -> None:
    """AC 8(d)/(e): every symbol and file the `?` path owned is gone, and init
    reaches for no agent or provider machinery."""
    import imp.cli.init as initmod
    import imp.cli.prompt as promptmod
    import imp.cli.pythonpath_detect as ppd
    from imp.assets import bundled_assets_dir

    assert not Path("src/imp/agents/interactive.py").exists()
    assert importlib.util.find_spec("imp.agents.interactive") is None
    assert not (bundled_assets_dir() / "init.md").exists()
    assert not hasattr(promptmod, "SETUP_EMPTY_PROMPT_HINT")
    assert not hasattr(ppd, "resolve_detection_provider")
    assert hasattr(ppd, "validate_detected_roots")     # the module itself stays
    for name in ("ScreenTalk", "_run_init_conversation", "_build_init_kickoff",
                 "_conversation_failure_message", "_workflow_choices"):
        assert not hasattr(initmod, name), name
    assert not hasattr(initmod._ScreenAction, "TALK")
    src = Path("src/imp/cli/init.py").read_text()
    assert "imp.agents" not in src                     # no agent machinery at all
    assert "imp.providers.resolve" not in src          # and no credential probe

"""`imp init` detects up front that git can build no author identity."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from imp.cli.init import (
    GitIdentityUnavailable,
    _git_can_commit,
    run_init,
    run_init_noninteractive,
)
from tests.conftest import _git


@pytest.fixture
def isolated_git_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Cut git off from the developer's config files and from any identity in the
    environment. The global path is a nonexistent file rather than /dev/null so a
    stray `git config --global` write stays observable."""
    global_config = tmp_path / "global-gitconfig"
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(var, raising=False)
    return global_config


@pytest.fixture
def broken_repo(tmp_path: Path, isolated_git_config: Path) -> Path:
    """A repo where git can resolve no author identity at all: no readable config,
    nothing in the environment, and GECOS/$EMAIL auto-detection disabled."""
    root = tmp_path / "broken"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.useConfigOnly", "true")
    return root


@pytest.fixture
def ordinary_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The everyday case: no repo-local identity, but the environment supplies one."""
    root = tmp_path / "ordinary"
    root.mkdir()
    _git(root, "init", "-q")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")
    return root


def test_probe_true_on_an_ordinary_repo(ordinary_repo: Path) -> None:
    assert _git_can_commit(ordinary_repo) is True


def test_probe_false_when_git_can_resolve_no_identity(broken_repo: Path) -> None:
    # The fixture only means anything if git really refuses to commit here.
    (broken_repo / "f.txt").write_text("x\n")
    _git(broken_repo, "add", "f.txt")
    assert _git(broken_repo, "commit", "-m", "x").returncode != 0

    assert _git_can_commit(broken_repo) is False


def test_probe_true_when_email_env_supplies_the_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_git_config: Path,
) -> None:
    root = tmp_path / "email-only"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Ada Lovelace")
    monkeypatch.setenv("EMAIL", "ada@example.org")
    # `git config --get user.email` reads only config files, so the wrong probe
    # would call this repo unconfigured even though `git commit` works fine.
    assert _git(root, "config", "--get", "user.email").returncode != 0

    assert _git_can_commit(root) is True


# The verbatim refusal copy, duplicated here on purpose: these tests pin the
# message a human reads, not whatever the constant happens to say.
_EXPECTED_REMEDY = (
    "git has no author identity here, so imp can't commit. "
    "Set one, then re-run `imp init`:\n"
    "\n"
    "  git config user.name 'Your Name'\n"
    "  git config user.email 'you@example.com'"
)


def test_noninteractive_fresh_path_refuses_and_writes_nothing(broken_repo: Path) -> None:
    with pytest.raises(GitIdentityUnavailable) as excinfo:
        run_init_noninteractive(
            broken_repo, output_fn=lambda _m: None, detect_verify_fn=lambda _r: None)

    assert str(excinfo.value) == _EXPECTED_REMEDY
    assert not (broken_repo / "imp").exists()


def test_noninteractive_already_initialized_path_refuses(broken_repo: Path) -> None:
    (broken_repo / "imp").mkdir()
    (broken_repo / "imp" / "config.yaml").write_text(
        "default-workflow: default\ndefault-base: master\n")

    with pytest.raises(GitIdentityUnavailable) as excinfo:
        run_init_noninteractive(
            broken_repo, output_fn=lambda _m: None, detect_verify_fn=lambda _r: None)

    assert str(excinfo.value) == _EXPECTED_REMEDY
    # The refusal must land before `git commit` runs, so the repo stays unborn.
    assert _git(broken_repo, "rev-parse", "--verify", "HEAD").returncode != 0


def test_cmd_init_exits_2_with_the_remedy_on_stderr(
    broken_repo: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.main as m

    monkeypatch.setattr(m, "_project_root", lambda: broken_repo)
    rc = m._cmd_init(argparse.Namespace(
        non_interactive=True, workflow=None, base=None, pythonpath=None))
    captured = capsys.readouterr()

    assert rc == 2
    assert captured.err.splitlines() == [
        "imp: git has no author identity here, so imp can't commit. "
        "Set one, then re-run `imp init`:",
        "",
        "  git config user.name 'Your Name'",
        "  git config user.email 'you@example.com'",
    ]
    assert not (broken_repo / "imp").exists()


# The verbatim interactive copy, duplicated here on purpose: these tests pin the
# text a human reads, not whatever the constants happen to say.
_LEAD_IN = (
    "git has no author identity here, so imp can't commit.\n"
    "imp will set one for this repo only — it goes in .git/config, not your machine."
)
_PROMPTS = ["  Your name: ", "  Your email: "]


class _Prompter:
    """An input_fn that hands back canned answers and records the prompts verbatim."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.replies.pop(0)


def _local_config(root: Path, key: str) -> str:
    """The repo-local value of `key`, or "" when unset."""
    return _git(root, "config", "--local", "--get", key).stdout.strip()


def _stub_interactive_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh interactive init runs the sandbox gate then the confirmation screen.
    Default them to "ready + accept as detected" so input_fn sees only the
    identity prompts."""
    import imp.cli.init as initmod

    monkeypatch.setattr(initmod, "sandbox_available", lambda: True, raising=False)
    monkeypatch.setattr(
        initmod, "_interactive_confirm_screen",
        lambda _root, state, **_kw: initmod.ScreenAccepted(state), raising=False)


def _run_interactive_init(root: Path, ask: _Prompter, msgs: list[str]) -> None:
    run_init(root, picker_fn=lambda _a: ["anthropic"], input_fn=ask,
             output_fn=msgs.append, detect_theme_fn=lambda: "ansi")


def test_fresh_interactive_init_asks_for_an_identity_and_commits(
    broken_repo: Path, isolated_git_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_interactive_gates(monkeypatch)
    ask = _Prompter("Ada Lovelace", "ada@example.org")
    msgs: list[str] = []

    _run_interactive_init(broken_repo, ask, msgs)

    assert _LEAD_IN in msgs
    assert ask.prompts == _PROMPTS
    assert _local_config(broken_repo, "user.name") == "Ada Lovelace"
    assert _local_config(broken_repo, "user.email") == "ada@example.org"
    assert not isolated_git_config.exists()
    assert "Set Ada Lovelace <ada@example.org> in .git/config." in msgs
    # The whole point: the init commit now succeeds, authored by the typed identity.
    author = _git(broken_repo, "log", "-1", "--format=%an <%ae>").stdout.strip()
    assert author == "Ada Lovelace <ada@example.org>"
    assert (broken_repo / "imp" / "config.yaml").is_file()


@pytest.mark.parametrize(
    ("name", "email"),
    [("", "ada@example.org"), ("Ada Lovelace", ""), ("   ", "ada@example.org")],
    ids=["empty-name", "empty-email", "blank-name"],
)
def test_an_empty_answer_refuses_and_writes_nothing(
    broken_repo: Path, monkeypatch: pytest.MonkeyPatch, name: str, email: str,
) -> None:
    _stub_interactive_gates(monkeypatch)
    ask = _Prompter(name, email)
    msgs: list[str] = []

    with pytest.raises(GitIdentityUnavailable) as excinfo:
        _run_interactive_init(broken_repo, ask, msgs)

    assert str(excinfo.value) == _EXPECTED_REMEDY
    assert _local_config(broken_repo, "user.name") == ""
    assert _local_config(broken_repo, "user.email") == ""


def test_identity_is_asked_before_the_confirmation_screen(
    broken_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Cancelling the screen must leave the human-typed identity behind, not a
    # half-applied setup — which only holds while the probe runs before it.
    import imp.cli.init as initmod

    def _cancel(_root: Path, _state: object, **_kw: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(initmod, "sandbox_available", lambda: True, raising=False)
    monkeypatch.setattr(initmod, "_interactive_confirm_screen", _cancel, raising=False)
    ask = _Prompter("Ada Lovelace", "ada@example.org")
    msgs: list[str] = []

    with pytest.raises(KeyboardInterrupt):
        _run_interactive_init(broken_repo, ask, msgs)

    assert ask.prompts == _PROMPTS
    assert _local_config(broken_repo, "user.name") == "Ada Lovelace"
    assert _local_config(broken_repo, "user.email") == "ada@example.org"


def test_reinit_asks_for_an_identity_and_commits(
    broken_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_interactive_gates(monkeypatch)
    (broken_repo / "imp").mkdir()
    (broken_repo / "imp" / "config.yaml").write_text(
        "default-workflow: default\ndefault-base: master\n")
    ask = _Prompter("Ada Lovelace", "ada@example.org")
    msgs: list[str] = []

    _run_interactive_init(broken_repo, ask, msgs)

    assert ask.prompts == _PROMPTS
    assert _local_config(broken_repo, "user.name") == "Ada Lovelace"
    assert _local_config(broken_repo, "user.email") == "ada@example.org"
    assert "Project already initialized (imp/config.yaml is committed)." in msgs
    assert "Committed imp/config.yaml." in msgs
    author = _git(broken_repo, "log", "-1", "--format=%an <%ae>").stdout.strip()
    assert author == "Ada Lovelace <ada@example.org>"


def _no_prompt(prompt: str) -> str:
    raise AssertionError(f"init prompted unexpectedly: {prompt!r}")


def test_ordinary_repo_interactive_init_never_asks_or_writes_an_identity(
    ordinary_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_interactive_gates(monkeypatch)
    msgs: list[str] = []

    run_init(ordinary_repo, picker_fn=lambda _a: ["anthropic"], input_fn=_no_prompt,
             output_fn=msgs.append, detect_theme_fn=lambda: "ansi")

    assert _local_config(ordinary_repo, "user.name") == ""
    assert _local_config(ordinary_repo, "user.email") == ""

    # The second run takes the already-initialized branch.
    msgs.clear()
    run_init(ordinary_repo, picker_fn=lambda _a: ["anthropic"], input_fn=_no_prompt,
             output_fn=msgs.append, detect_theme_fn=lambda: "ansi")

    assert "Project already initialized (imp/config.yaml is committed)." in msgs
    assert _local_config(ordinary_repo, "user.name") == ""
    assert _local_config(ordinary_repo, "user.email") == ""


def test_ordinary_repo_noninteractive_init_writes_no_identity(ordinary_repo: Path) -> None:
    msgs: list[str] = []

    run_init_noninteractive(
        ordinary_repo, output_fn=msgs.append, detect_verify_fn=lambda _r: None)

    assert _local_config(ordinary_repo, "user.name") == ""
    assert _local_config(ordinary_repo, "user.email") == ""

    msgs.clear()
    run_init_noninteractive(
        ordinary_repo, output_fn=msgs.append, detect_verify_fn=lambda _r: None)

    assert "Project already initialized (imp/config.yaml is committed)." in msgs
    assert _local_config(ordinary_repo, "user.name") == ""
    assert _local_config(ordinary_repo, "user.email") == ""

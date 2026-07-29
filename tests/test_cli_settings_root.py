"""The CLI's settings root is the MAIN CHECKOUT, even when the command runs from
inside a quest worktree (spec §6, AC 7). A branch must not be able to edit — or
be shown a different set of — the settings that judge it.

The fixture builds a real linked worktree, so nothing but the git common dir can
tell the two roots apart: a stubbed root would pin the wiring, not the behavior.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from imp.cli.main import main
from imp.paths import display_path, project_state_dir
from imp.providers import register_builtin_providers, resolve
from tests._init_config import write_real_init_config
from tests.conftest import _git


@pytest.fixture(autouse=True)
def _builtin_providers() -> None:
    # Reset then re-register so stale fakes from other workers can't leak in.
    resolve.reset_providers_for_tests()
    register_builtin_providers()


@pytest.fixture
def checkouts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """(main checkout, linked worktree), cwd parked in the worktree. The main
    checkout carries `imp init`'s real bytes (test_cmd_fast: "make test"); the
    worktree's copy is edited to disagree with it."""
    control = tmp_path / "proj"
    control.mkdir()
    write_real_init_config(control)      # git init + commit imp/config.yaml on main
    work = tmp_path / "wt"
    # conftest's _git runs check=False; assert here so a git failure is legible
    # instead of surfacing as a missing-file error two lines down.
    added = _git(control, "worktree", "add", "-q", "-b", "imp/q", str(work), "main")
    assert added.returncode == 0, added.stderr
    (work / "imp" / "config.yaml").write_text('test_cmd_fast: "branch lies"\n')
    monkeypatch.chdir(work)
    return control, work


def test_config_set_project_from_a_worktree_writes_the_main_checkout(
    checkouts: tuple[Path, Path], capsys: pytest.CaptureFixture[str],
) -> None:
    control, work = checkouts

    assert main(["config", "set", "--project", "test_cmd_fast", "make gate"]) == 0

    # `(was "make test")` is the main checkout's committed value, so the read
    # side came from there too — not from the worktree file the cwd sits in.
    assert capsys.readouterr().out == (
        'imp/config.yaml: test_cmd_fast: "make gate" (was "make test")\n')
    assert 'test_cmd_fast: "make gate"' in (control / "imp" / "config.yaml").read_text()
    assert (work / "imp" / "config.yaml").read_text() == 'test_cmd_fast: "branch lies"\n'


def test_config_from_a_worktree_shows_the_main_checkout(
    checkouts: tuple[Path, Path], capsys: pytest.CaptureFixture[str],
) -> None:
    control, _ = checkouts

    assert main(["config"]) == 0

    out = capsys.readouterr().out
    assert "make test" in out         # the main checkout's committed value
    assert "branch lies" not in out   # the worktree's file is not a layer
    lines = out.splitlines()
    assert lines[0] == (
        f"project state: {display_path(project_state_dir(control))}")
    assert lines[1] == ""


def test_the_active_theme_comes_from_the_main_checkout(
    checkouts: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`theme` is a setting like any other: the worktree's copy must not repaint
    a command that runs inside it."""
    from imp.cli import palette as pal
    from imp.cli.main import _apply_theme
    control, work = checkouts
    monkeypatch.delenv("IMP_THEME", raising=False)
    (control / "imp" / "config.yaml").write_text("theme: dark\n")
    (work / "imp" / "config.yaml").write_text("theme: light\n")

    _apply_theme()

    assert pal.active_theme() == "dark"


def test_the_config_writer_takes_one_root(tmp_path: Path) -> None:
    """No parameter a caller can aim at a worktree: `project_root` is removed,
    not repointed (spec §1's rule, applied to the writer)."""
    from imp.cli.config_write import run_set, run_unset

    with pytest.raises(TypeError, match="project_root"):
        run_set(  # type: ignore[call-arg]
            key="test_cmd_fast", value="x", scope="project",
            project_root=tmp_path, control_root=tmp_path)
    with pytest.raises(TypeError, match="project_root"):
        run_unset(  # type: ignore[call-arg]
            key="test_cmd_fast", scope="project",
            project_root=tmp_path, control_root=tmp_path)

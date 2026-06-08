# tests/test_cli_config_divergence.py
"""Runtime notice when `imp` is launched inside a *linked* git worktree whose
committed imp/config.yaml differs from the control root's. imp always routes from
the control root's config (per-change-worktrees design §5.3/§5.4); without this
notice that precedence is silent — a codex-only config in the launch worktree
runs on whatever the main checkout lists, with no signal to the user."""
import subprocess
from pathlib import Path

from imp.cli.main import _config_divergence_notice


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False,
    )


def _init_repo(root: Path, config: str) -> None:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".imp/\n")
    (root / "imp").mkdir()
    (root / "imp" / "config.yaml").write_text(config)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")


def _add_linked_worktree(control: Path, path: Path, config: str | None) -> None:
    _git(control, "worktree", "add", "-q", "-b", "side", str(path))
    if config is not None:
        (path / "imp" / "config.yaml").write_text(config)
        _git(path, "commit", "-qm", "diverge", "imp/config.yaml")


def test_no_notice_when_launched_in_control_root(tmp_path: Path) -> None:
    _init_repo(tmp_path, "providers: [claude_code]\n")
    assert _config_divergence_notice(tmp_path, tmp_path) is None


def test_no_notice_when_linked_worktree_config_matches(tmp_path: Path) -> None:
    control = tmp_path / "main"
    control.mkdir()
    _init_repo(control, "providers: [claude_code]\n")
    wt = tmp_path / "wt"
    _add_linked_worktree(control, wt, config=None)  # same committed config
    assert _config_divergence_notice(control, wt) is None


def test_notice_when_linked_worktree_config_differs(tmp_path: Path) -> None:
    control = tmp_path / "main"
    control.mkdir()
    _init_repo(control, "providers: [claude_code]\n")
    wt = tmp_path / "wt"
    _add_linked_worktree(control, wt, config="providers: [codex]\n")

    notice = _config_divergence_notice(control, wt)
    assert notice is not None
    # Points at the config that ACTUALLY routes the run (the control root's)...
    assert str(control / "imp" / "config.yaml") in notice
    # ...and makes clear the launch worktree's own config is not consulted.
    assert "not used" in notice


def test_notice_resolves_from_a_subdir_of_the_linked_worktree(tmp_path: Path) -> None:
    control = tmp_path / "main"
    control.mkdir()
    _init_repo(control, "providers: [claude_code]\n")
    wt = tmp_path / "wt"
    _add_linked_worktree(control, wt, config="providers: [codex]\n")
    sub = wt / "deep" / "nested"
    sub.mkdir(parents=True)
    assert _config_divergence_notice(control, sub) is not None

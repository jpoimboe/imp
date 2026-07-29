"""One-shot startup migration of the global config file (spec scope 5, step 1).

The steps are plain functions over explicit paths, so the criteria are exercised
directly rather than by hoping a main([...]) call fires them."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from imp.cli.config_migrate import (
    migrate_global_config,
    migrate_project_config,
    run_startup_migrations,
)
from imp.paths import display_path
from tests.conftest import _assert_outside_a_git_work_tree


def _legacy(tmp_path: Path, body: str = "providers: [openai]\n") -> Path:
    p = tmp_path / "legacy" / "imp" / "config.yaml"
    p.parent.mkdir(parents=True)
    p.write_text(body)
    return p


def test_migrates_once_and_reports_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    legacy = _legacy(tmp_path)
    new = tmp_path / "home" / "config.yaml"
    migrate_global_config(legacy, new)
    assert new.read_text() == "providers: [openai]\n"
    assert not legacy.exists()
    assert not legacy.parent.exists()      # the emptied legacy dir is removed
    # Spec scope 9 pins this line verbatim, prefix and connector included. The
    # paths go through display_path because a tmp dir may itself live under $HOME.
    err = capsys.readouterr().err
    assert err == f"imp: moved {display_path(legacy)} to {display_path(new)}\n"


def test_second_call_is_a_silent_no_op(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    legacy = _legacy(tmp_path)
    new = tmp_path / "home" / "config.yaml"
    migrate_global_config(legacy, new)
    capsys.readouterr()
    migrate_global_config(legacy, new)
    assert capsys.readouterr().err == ""
    assert new.read_text() == "providers: [openai]\n"


def test_existing_new_file_wins_and_legacy_is_left_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    legacy = _legacy(tmp_path)
    new = tmp_path / "home" / "config.yaml"
    new.parent.mkdir(parents=True)
    new.write_text("providers: [anthropic]\n")
    migrate_global_config(legacy, new)
    assert new.read_text() == "providers: [anthropic]\n"
    assert legacy.read_text() == "providers: [openai]\n"
    assert capsys.readouterr().err == ""


def test_absent_legacy_creates_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    new = tmp_path / "home" / "config.yaml"
    migrate_global_config(tmp_path / "legacy" / "imp" / "config.yaml", new)
    assert not new.parent.exists()
    assert capsys.readouterr().err == ""


def test_non_empty_legacy_dir_survives_the_move(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    legacy = _legacy(tmp_path)
    (legacy.parent / "other.yaml").write_text("x: 1\n")
    new = tmp_path / "home" / "config.yaml"
    migrate_global_config(legacy, new)
    assert new.is_file()
    assert (legacy.parent / "other.yaml").is_file()
    assert capsys.readouterr().err.startswith("imp: moved ")


def test_run_startup_migrations_resolves_the_defaults_from_the_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)   # never resolve the project step against the real checkout
    _assert_outside_a_git_work_tree(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "legacy"))
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "home"))
    legacy = _legacy(tmp_path, "theme: dark\n")
    run_startup_migrations()
    assert (tmp_path / "home" / "config.yaml").read_text() == "theme: dark\n"
    assert not legacy.exists()


def test_run_startup_migrations_swallows_a_failing_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A migration that cannot run is retried next invocation; it must never be
    # the reason a command dies. Here IMP_HOME's parent is a regular file, so
    # creating the new home raises NotADirectoryError.
    monkeypatch.chdir(tmp_path)   # never resolve the project step against the real checkout
    _assert_outside_a_git_work_tree(tmp_path)
    (tmp_path / "blocked").write_text("not a directory")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "legacy"))
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "blocked" / "home"))
    legacy = _legacy(tmp_path)
    run_startup_migrations()   # must not raise
    assert legacy.is_file()


def test_legacy_global_config_path_still_honors_xdg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The migration's source, and the only remaining XDG_CONFIG_HOME reader in src/.
    from imp.providers.paths import legacy_global_config_path

    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/x")
    assert legacy_global_config_path() == Path("/tmp/x/imp/config.yaml")


def test_display_path_is_home_relative_under_home_and_absolute_outside() -> None:
    # Together with the notice assertion above, this is what pins the mandated
    # copy `imp: moved ~/.config/imp/config.yaml to ~/.imp/config.yaml`.
    assert display_path(Path.home() / ".imp" / "config.yaml") == "~/.imp/config.yaml"
    assert display_path(Path.home() / ".config" / "imp" / "config.yaml") == (
        "~/.config/imp/config.yaml")
    assert display_path(Path("/etc/imp/config.yaml")) == "/etc/imp/config.yaml"


def test_entrypoint_migrates_before_delegating_to_the_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import signal

    calls: list[str] = []
    monkeypatch.setattr("imp.cli.config_migrate.run_startup_migrations",
                        lambda: calls.append("migrate"))
    monkeypatch.setattr("imp.cli.main.main", lambda: calls.append("cli") or 0)
    # Importing the entrypoint installs its module-level SIGINT handler; restore
    # the prior one so Ctrl-C behavior doesn't leak into the rest of the session.
    prior = signal.getsignal(signal.SIGINT)
    try:
        import imp.__main__ as entrypoint
        assert entrypoint.main() == 0
    finally:
        signal.signal(signal.SIGINT, prior)
    assert calls == ["migrate", "cli"]


def test_cli_main_does_not_migrate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The suite drives imp.cli.main.main in-process at 168 sites; a migration
    # there would rearrange the developer's own files mid-run.
    from imp.cli.main import main as cli_main

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "legacy"))
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "home"))
    legacy = _legacy(tmp_path)
    assert cli_main([]) == 0
    capsys.readouterr()
    assert legacy.is_file()
    assert not (tmp_path / "home" / "config.yaml").exists()


_RENAME_NOTICE = ("imp: renamed imp/project.yaml to imp/config.yaml (staged) — "
                  "commit it to finish the move\n")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(root), *args],
                          check=True, capture_output=True, text=True)


def _staged_rename(root: Path) -> str:
    """Porcelain status with rename detection pinned on. Left to the ambient
    config, a developer's or CI's `status.renames=false` reports the staged
    rename as a separate delete plus add and fails the caller for no reason."""
    return _git(root, "-c", "status.renames=true",
                "status", "--porcelain").stdout.strip()


def _old_name_git_repo(root: Path) -> Path:
    """A committed git repo still carrying imp/project.yaml."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "imp").mkdir()
    (root / "imp" / "project.yaml").write_text("default-workflow: default\n")
    # `imp init` gitignores .imp/ wholesale, and the worktree test below adds a
    # linked worktree under it — without this the main checkout reports `?? .imp/`.
    (root / ".gitignore").write_text(".imp/\n.imp-isolated/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def test_project_rename_is_staged_not_committed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _old_name_git_repo(tmp_path / "repo")
    migrate_project_config(repo)
    assert (repo / "imp" / "config.yaml").read_text() == "default-workflow: default\n"
    assert not (repo / "imp" / "project.yaml").exists()
    # Staged, not committed: git sees the rename in the index and the history
    # still has exactly the one seed commit.
    assert _staged_rename(repo) == "R  imp/project.yaml -> imp/config.yaml"
    assert _git(repo, "rev-list", "--count", "HEAD").stdout.strip() == "1"
    assert capsys.readouterr().err == _RENAME_NOTICE


def test_project_rename_fires_from_a_subdirectory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    # find_project_root's marker is imp/config.yaml now, so it can no longer
    # locate an un-migrated repo at all — the step must resolve with git.
    repo = _old_name_git_repo(tmp_path / "repo")
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    migrate_project_config(sub)
    assert (repo / "imp" / "config.yaml").is_file()
    assert capsys.readouterr().err == _RENAME_NOTICE


def test_project_rename_is_a_no_op_once_config_yaml_exists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _old_name_git_repo(tmp_path / "repo")
    migrate_project_config(repo)
    capsys.readouterr()
    migrate_project_config(repo)
    assert capsys.readouterr().err == ""
    assert _staged_rename(repo) == "R  imp/project.yaml -> imp/config.yaml"


def test_project_rename_never_fires_inside_a_quest_worktree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    # Inside a linked worktree `git rev-parse --show-toplevel` is the worktree
    # while find_control_root is the main checkout, so the step declines and the
    # rename can never fire mid-run.
    repo = _old_name_git_repo(tmp_path / "repo")
    wt = repo / ".imp" / "worktrees" / "x"
    _git(repo, "worktree", "add", "-q", "-b", "wt", str(wt), "main")
    migrate_project_config(wt)
    assert (wt / "imp" / "project.yaml").is_file()
    assert not (wt / "imp" / "config.yaml").exists()
    assert (repo / "imp" / "project.yaml").is_file()
    # Nothing staged in the main checkout. Asserted as the rename's absence, not
    # exact-equality against "": `git worktree add` materializes the .imp/ dir,
    # which only the seeded .gitignore keeps out of `status` at all.
    assert "imp/project.yaml" not in _git(repo, "status", "--porcelain").stdout
    assert capsys.readouterr().err == ""


def test_project_rename_is_a_no_op_outside_a_git_work_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # find_control_root raises PreconditionError here; run_startup_migrations
    # swallows it and the stray old-name file is left exactly as it was.
    monkeypatch.chdir(tmp_path)
    _assert_outside_a_git_work_tree(tmp_path)
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "home"))
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "project.yaml").write_text("default-workflow: default\n")
    run_startup_migrations()
    assert (tmp_path / "imp" / "project.yaml").is_file()
    assert not (tmp_path / "imp" / "config.yaml").exists()
    assert capsys.readouterr().err == ""


def test_run_startup_migrations_stages_the_rename_at_the_process_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "legacy"))
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "home"))
    repo = _old_name_git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    run_startup_migrations()
    assert (repo / "imp" / "config.yaml").is_file()
    assert capsys.readouterr().err == _RENAME_NOTICE


def test_a_failing_global_step_does_not_block_the_project_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Each step is independently best-effort: a permanently failing step 1 must
    # not leave a user's repo un-migrated forever.
    (tmp_path / "blocked").write_text("not a directory")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "legacy"))
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "blocked" / "home"))
    _legacy(tmp_path)
    repo = _old_name_git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    run_startup_migrations()
    assert (repo / "imp" / "config.yaml").is_file()
    assert capsys.readouterr().err == _RENAME_NOTICE


def test_project_rename_spawns_no_subprocess_once_migrated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _old_name_git_repo(tmp_path / "repo")
    migrate_project_config(repo)
    assert (repo / "imp" / "config.yaml").is_file()
    # All git-based setup is done; now block every subprocess.run so that a
    # stray git spawn causes a hard failure.
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("unexpected subprocess.run after migration")))
    migrate_project_config(repo)  # must return without spawning git


def test_cli_main_does_not_stage_the_project_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from imp.cli.main import main as cli_main

    monkeypatch.setenv("IMP_HOME", str(tmp_path / "home"))
    repo = _old_name_git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    assert cli_main([]) == 0
    capsys.readouterr()
    assert (repo / "imp" / "project.yaml").is_file()
    assert "imp/project.yaml" not in _git(repo, "status", "--porcelain").stdout


def test_config_paths_module_is_stdlib_only(tmp_path: Path) -> None:
    """The startup migration resolves these paths before any command dispatch, so
    importing them must not drag in the yaml/provider stack."""
    code = (
        "import sys\n"
        "from imp.providers.paths import global_config_path, legacy_global_config_path\n"
        "heavy = sorted({n.split('.')[0] for n in sys.modules} & {'ruamel', 'yaml'})\n"
        "print('HEAVY:' + ','.join(heavy))\n"
        "print('GLOBAL:' + str(global_config_path()))\n"
        "print('LEGACY:' + str(legacy_global_config_path()))\n"
    )
    env = {**os.environ, "IMP_HOME": str(tmp_path / "h"),
           "XDG_CONFIG_HOME": str(tmp_path / "x")}
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, env=env, cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    reported = {
        key: value
        for key, _, value in (ln.partition(":") for ln in proc.stdout.splitlines())
        if key in {"HEAVY", "GLOBAL", "LEGACY"}
    }
    assert reported.keys() == {"HEAVY", "GLOBAL", "LEGACY"}, (
        f"child did not report all three lines; stdout was {proc.stdout!r}"
    )
    assert reported["HEAVY"] == "", f"imp.providers.paths pulled in {reported['HEAVY']}"
    assert reported["GLOBAL"] == str(tmp_path / "h" / "config.yaml")
    assert reported["LEGACY"] == str(tmp_path / "x" / "imp" / "config.yaml")

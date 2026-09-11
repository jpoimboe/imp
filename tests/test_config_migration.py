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
    migrate_config_layers,
    migrate_global_config,
    migrate_project_config,
    migrate_spike_artifact_refs,
    rewrite_config_file,
    run_startup_migrations,
)
from imp.engine.worktree import RepoGitLock, _git_ok, artifact_commit
from imp.paths import display_path, project_state_dir
from tests.conftest import _assert_outside_a_git_work_tree, _init_repo


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


_LEGACY_BODY = (
    "# the gate\n"
    'test_cmd_fast: ""\n'
    "test_cmd_slow: make slow\n"
    "# import roots\n"
    "pythonpath: []\n"
)

_RETIRED_BODY = "default-workflow: imp\ncontext:\n  checkpoint_threshold: 260000\n"


def _removed(path: Path) -> str:
    return ("imp: removed the retired context.checkpoint_threshold setting "
            f"from {display_path(path)}\n")


def _by_hand(path: Path) -> str:
    return ("imp: remove the retired context.checkpoint_threshold setting "
            f"from {display_path(path)} by hand\n")


def test_rewrites_both_legacy_spellings_in_one_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from imp.providers.config import parse_config_layer

    cfg = tmp_path / "config.yaml"
    cfg.write_text(_LEGACY_BODY)
    rewrite_config_file(cfg)
    text = cfg.read_text()
    assert "test_cmd_fast: false" in text
    assert "pythonpath: false" in text
    assert "# the gate" in text and "# import roots" in text     # comments intact
    assert (text.index("test_cmd_fast") < text.index("test_cmd_slow")
            < text.index("pythonpath"))                          # key order intact
    layer, _src = parse_config_layer(cfg)                        # type: ignore[misc]
    assert layer == {"test_cmd_fast": False, "test_cmd_slow": "make slow",
                     "pythonpath": False}
    assert capsys.readouterr().err == (
        f'imp: rewrote "" to false in {display_path(cfg)} (test_cmd_fast)\n'
        f"imp: rewrote [] to false in {display_path(cfg)} (pythonpath)\n")


def test_rewriting_a_second_time_leaves_the_file_byte_identical(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_LEGACY_BODY)
    rewrite_config_file(cfg)
    once = cfg.read_text()
    capsys.readouterr()
    rewrite_config_file(cfg)
    assert cfg.read_text() == once
    assert capsys.readouterr().err == ""


def test_worktree_setup_and_single_quotes_are_covered(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("worktree_setup: ''  # trailing note\n")
    rewrite_config_file(cfg)
    assert "worktree_setup: false" in cfg.read_text()


def test_a_nested_key_is_left_alone(tmp_path: Path) -> None:
    # apply_set can only fix a TOP-LEVEL key: matching an indented one would add a
    # second key beside the untouched legacy value and re-announce it forever.
    cfg = tmp_path / "config.yaml"
    body = 'vars:\n  test_cmd_fast: ""\n'
    cfg.write_text(body)
    rewrite_config_file(cfg)
    assert cfg.read_text() == body


def test_each_key_is_matched_only_against_its_own_legacy_spelling(
    tmp_path: Path,
) -> None:
    # `pythonpath: ""` and `test_cmd_fast: []` are spellings imp never wrote; they
    # are hand-written errors for the reader to reject, not migrations — and
    # migrating them would print a notice naming the wrong token.
    # `test_cmd_fast: ""x` has a non-empty suffix after the quotes: not legacy.
    cfg = tmp_path / "config.yaml"
    body = 'pythonpath: ""\ntest_cmd_fast: []\nworktree_setup: ""x\n'
    cfg.write_text(body)
    rewrite_config_file(cfg)
    assert cfg.read_text() == body


def test_crlf_line_endings_are_rewritten(tmp_path: Path) -> None:
    from imp.providers.config import parse_config_layer

    cfg = tmp_path / "config.yaml"
    cfg.write_bytes(b'test_cmd_fast: ""\r\ntest_cmd_slow: make check\r\n')
    rewrite_config_file(cfg)
    text = cfg.read_text()
    assert "test_cmd_fast: false" in text
    layer, _src = parse_config_layer(cfg)  # type: ignore[misc]
    assert layer.get("test_cmd_fast") is False


def test_a_clean_file_is_never_parsed_and_a_missing_file_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("the byte pre-check must short-circuit a clean file")

    monkeypatch.setattr("imp.cli.config_edit.apply_set", _boom)
    monkeypatch.setattr("imp.cli.config_edit.apply_unset", _boom)
    cfg = tmp_path / "config.yaml"
    body = 'test_cmd_fast: "make check"\npythonpath: [src]\n'
    cfg.write_text(body)
    rewrite_config_file(cfg)
    assert cfg.read_text() == body
    rewrite_config_file(tmp_path / "nope.yaml")      # missing: not an error


def test_the_rewrite_is_not_staged(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "imp").mkdir()
    (repo / "imp" / "config.yaml").write_text('test_cmd_fast: ""\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    rewrite_config_file(repo / "imp" / "config.yaml")
    assert _git(repo, "diff", "--cached", "--name-only").stdout == ""
    assert _git(repo, "diff", "--name-only").stdout == "imp/config.yaml\n"


def test_all_three_config_locations_are_migrated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    (home / "projects" / "proj").mkdir(parents=True)
    (home / "config.yaml").write_text('test_cmd_slow: ""\n')
    (home / "projects" / "proj" / "config.yaml").write_text("pythonpath: []\n")
    monkeypatch.setenv("IMP_HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "imp").mkdir()
    (repo / "imp" / "config.yaml").write_text(
        'worktree_setup: ""\ncontext:\n  checkpoint_threshold: 260000\n')
    monkeypatch.chdir(repo)

    migrate_config_layers(repo)

    checkout = (repo / "imp" / "config.yaml").read_text()
    assert "worktree_setup: false" in checkout
    assert "# context:" in checkout          # the widened checkout gate found it
    assert "test_cmd_slow: false" in (home / "config.yaml").read_text()
    assert "pythonpath: false" in (
        home / "projects" / "proj" / "config.yaml").read_text()


def test_a_failing_checkout_step_does_not_block_the_home_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Resolving the checkout needs git and can raise; the ~/.imp rewrites must
    # already have happened, matching this module's step-independent neighbours.
    from imp.engine.worktree import PreconditionError

    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text('test_cmd_fast: ""\n')
    monkeypatch.setenv("IMP_HOME", str(home))
    outside = tmp_path / "outside"
    (outside / "imp").mkdir(parents=True)
    _assert_outside_a_git_work_tree(outside)
    (outside / "imp" / "config.yaml").write_text("pythonpath: []\n")

    with pytest.raises(PreconditionError):
        migrate_config_layers(outside)

    assert (home / "config.yaml").read_text() == "test_cmd_fast: false\n"
    assert (outside / "imp" / "config.yaml").read_text() == "pythonpath: []\n"


def test_run_startup_migrations_rewrites_the_global_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _assert_outside_a_git_work_tree(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "legacy"))
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "home"))
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text('test_cmd_fast: ""\n')
    run_startup_migrations()
    assert (home / "config.yaml").read_text() == "test_cmd_fast: false\n"


def test_the_retired_key_is_stripped_and_announced(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from imp.providers.config import parse_config_layer

    cfg = tmp_path / "config.yaml"
    cfg.write_text(_RETIRED_BODY)
    rewrite_config_file(cfg)
    # apply_unset comments the key out and climbs: a `context:` left with no live
    # child goes too, or the file keeps an empty mapping the reader must judge.
    assert cfg.read_text() == (
        "default-workflow: imp\n# context:\n#   checkpoint_threshold: 260000\n")
    layer, _src = parse_config_layer(cfg)                        # type: ignore[misc]
    assert layer == {"default-workflow": "imp"}                  # loads clean
    assert capsys.readouterr().err == _removed(cfg)


def test_every_commented_shape_migrates_and_loads_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from imp.providers.config import parse_config_layer

    shapes = {
        "block": ("context:\n  checkpoint_threshold: 260000\n"
                  "  max_reincarnations: 3\n", {"max_reincarnations": 3}),
        "flow": ("default-workflow: imp\n"
                 "context: {checkpoint_threshold: 260000}\n", None),
        "quoted": ('context:\n  "checkpoint_threshold": 260000\n', None),
    }
    for name, (body, expected) in shapes.items():
        cfg = tmp_path / name / "config.yaml"
        cfg.parent.mkdir()
        cfg.write_text(body)
        rewrite_config_file(cfg)
        layer, _src = parse_config_layer(cfg)                    # type: ignore[misc]
        assert layer.get("context") == expected, name
        assert capsys.readouterr().err == _removed(cfg), name


def test_a_second_run_over_a_migrated_file_is_a_silent_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The commented-out line still contains the identifier: the pre-check asks
    # for a LIVE key, so coverage and idempotence are the same predicate.
    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("a migrated file must never be parsed again")

    cfg = tmp_path / "config.yaml"
    cfg.write_text(_RETIRED_BODY)
    rewrite_config_file(cfg)
    once = cfg.read_text()
    capsys.readouterr()
    monkeypatch.setattr("imp.cli.config_edit.apply_unset", _boom)
    monkeypatch.setattr("imp.cli.config_edit.apply_set", _boom)
    rewrite_config_file(cfg)
    assert cfg.read_text() == once
    assert capsys.readouterr().err == ""


def test_the_pre_check_ignores_lookalikes_and_commented_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("a file with no live retired key must not be parsed")

    monkeypatch.setattr("imp.cli.config_edit.apply_unset", _boom)
    cfg = tmp_path / "config.yaml"
    for body in ("vars:\n  my_checkpoint_threshold: 1\n",
                 "vars:\n  a.checkpoint_threshold: 2\n",
                 "context:\n#   checkpoint_threshold: 260000\n"):
        cfg.write_text(body)
        rewrite_config_file(cfg)
        assert cfg.read_text() == body
    assert capsys.readouterr().err == ""


def test_an_unstrippable_key_is_left_alone_with_a_manual_edit_notice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    shapes = {
        "flow_multi": "context: {\n  checkpoint_threshold: 260000}\n",
        "shared": "context: {checkpoint_threshold: 260000, max_reincarnations: 3}\n",
    }
    for name, body in shapes.items():
        cfg = tmp_path / name / "config.yaml"
        cfg.parent.mkdir()
        cfg.write_text(body)
        rewrite_config_file(cfg)
        assert cfg.read_text() == body, name
        assert capsys.readouterr().err == _by_hand(cfg), name
        # The key is still live, so the pointer repeats until a human fixes it.
        rewrite_config_file(cfg)
        assert cfg.read_text() == body, name
        assert capsys.readouterr().err == _by_hand(cfg), name


def test_a_legacy_spelling_beside_an_unstrippable_key_lands_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = tmp_path / "config.yaml"
    body = ('test_cmd_fast: ""\n'
            "context: {checkpoint_threshold: 260000, max_reincarnations: 3}\n")
    cfg.write_text(body)
    rewrite_config_file(cfg)
    # One gate: the spelling fix cannot land over text the reader still rejects.
    assert cfg.read_text() == body
    assert capsys.readouterr().err == _by_hand(cfg)

    cfg.write_text('test_cmd_fast: ""\ncontext: {max_reincarnations: 3}\n')
    rewrite_config_file(cfg)
    assert "test_cmd_fast: false" in cfg.read_text()
    assert capsys.readouterr().err == (
        f'imp: rewrote "" to false in {display_path(cfg)} (test_cmd_fast)\n')


def test_a_layer_carrying_both_defects_heals_in_one_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The regression guard for the shared landing gate: verify_readable parses
    # the WHOLE candidate, so with a gate per transform each one aborts on the
    # other's defect, order-independently, and the install never self-heals.
    from imp.providers.config import parse_config_layer

    monkeypatch.chdir(tmp_path)
    _assert_outside_a_git_work_tree(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "legacy"))
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "home"))
    home = tmp_path / "home"
    home.mkdir()
    cfg = home / "config.yaml"
    cfg.write_text('test_cmd_fast: ""\ncontext:\n  checkpoint_threshold: 260000\n'
                   "  max_reincarnations: 3\n")

    run_startup_migrations()

    layer, _src = parse_config_layer(cfg)                        # type: ignore[misc]
    assert layer == {"test_cmd_fast": False, "context": {"max_reincarnations": 3}}
    assert capsys.readouterr().err == (
        _removed(cfg)
        + f'imp: rewrote "" to false in {display_path(cfg)} (test_cmd_fast)\n')


def test_a_quest_worktrees_own_config_is_never_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("IMP_HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "imp").mkdir()
    (repo / "imp" / "config.yaml").write_text("default-workflow: imp\n")
    (repo / ".gitignore").write_text(".imp/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    wt = repo / ".imp" / "worktrees" / "x"
    _git(repo, "worktree", "add", "-q", "-b", "wt", str(wt), "main")
    (wt / "imp" / "config.yaml").write_text(_RETIRED_BODY)

    migrate_config_layers(wt)

    # The checkout layer resolves through find_control_root, so a worktree's own
    # copy is never the file that gets rewritten mid-run.
    assert (wt / "imp" / "config.yaml").read_text() == _RETIRED_BODY
    assert (repo / "imp" / "config.yaml").read_text() == "default-workflow: imp\n"
    assert capsys.readouterr().err == ""


def _spiked_repo(tmp_path: Path) -> Path:
    """A checkout whose one artifact ref carries parked spike evidence."""
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    probe = tmp_path / "files" / "probe.py"
    probe.parent.mkdir(parents=True)
    probe.write_text("print(1)\n")
    spec = tmp_path / "files" / "f.md"
    spec.write_text("# spec\n")
    artifact_commit(root, "aaa", {"spec/f.md": spec, "spike/probe.py": probe},
                    message="artifacts: threshold")
    return root


def _marker(root: Path) -> Path:
    return project_state_dir(root) / "spike-refs-pruned"


def _tip_entries(root: Path) -> list[str]:
    return _git_ok(root, "ls-tree", "--name-only", "refs/imp/artifacts/aaa").split()


_PRUNED_ONE = ("imp: pruned spike evidence from 1 artifact ref; "
               "run `git gc` to reclaim the space.\n")
_PRUNED_MANY = ("imp: pruned spike evidence from 26 artifact refs; "
                "run `git gc` to reclaim the space.\n")


def test_the_spike_sweep_prunes_the_ref_marks_it_done_and_reports_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    root = _spiked_repo(tmp_path)

    migrate_spike_artifact_refs(root)

    assert _tip_entries(root) == ["spec"]
    assert _marker(root).is_file()
    assert capsys.readouterr().err == _PRUNED_ONE


def test_the_plural_form_names_the_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _spiked_repo(tmp_path)
    monkeypatch.setattr("imp.engine.worktree.prune_spike_from_artifact_refs",
                        lambda control_root: 26)

    migrate_spike_artifact_refs(root)

    assert capsys.readouterr().err == _PRUNED_MANY


def test_a_sweep_that_rewrote_nothing_says_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)

    migrate_spike_artifact_refs(root)

    assert _marker(root).is_file()
    assert capsys.readouterr().err == ""


def test_the_marker_stops_a_second_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _spiked_repo(tmp_path)
    _marker(root).touch()

    def never(control_root: Path) -> int:
        raise AssertionError("swept with the marker already present")

    monkeypatch.setattr("imp.engine.worktree.prune_spike_from_artifact_refs", never)

    migrate_spike_artifact_refs(root)

    assert _tip_entries(root) == ["spec", "spike"]
    assert capsys.readouterr().err == ""


def test_a_held_lock_defers_the_sweep_and_the_next_invocation_retries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    root = _spiked_repo(tmp_path)
    with RepoGitLock(root).acquire(timeout=10):
        migrate_spike_artifact_refs(root)
        assert not _marker(root).exists()
        assert _tip_entries(root) == ["spec", "spike"]
        assert capsys.readouterr().err == ""

    migrate_spike_artifact_refs(root)

    assert _marker(root).is_file()
    assert _tip_entries(root) == ["spec"]
    assert capsys.readouterr().err == _PRUNED_ONE


def test_a_raising_sweep_cannot_fail_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _spiked_repo(tmp_path)
    monkeypatch.chdir(root)

    def boom(cwd: Path) -> None:
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr("imp.cli.config_migrate.migrate_spike_artifact_refs", boom)

    run_startup_migrations()   # must not raise

    assert not _marker(root).exists()


def test_the_spike_sweep_runs_after_the_state_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _spiked_repo(tmp_path)
    monkeypatch.chdir(root)
    order: list[str] = []
    monkeypatch.setattr("imp.cli.state_migrate.migrate_project_state",
                        lambda cwd: order.append("state"))
    monkeypatch.setattr("imp.cli.config_migrate.migrate_spike_artifact_refs",
                        lambda cwd: order.append("spike"))

    run_startup_migrations()

    assert order == ["state", "spike"]

"""The project layer errors instead of loading empty when a repo is still on the
pre-rename `imp/project.yaml` (spec scope 6). A quest branched before the rename
and resumed after it would otherwise silently lose its whole project config."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from imp.engine.errors import LoadError
from imp.engine.worktree import checked_out_branch
from imp.providers.effective import (
    load_effective_config,
    project_rename_hint,
    project_rename_pending,
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args],
                   check=True, capture_output=True, text=True)


def _old_name_repo(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "imp").mkdir(parents=True)
    (proj / "imp" / "project.yaml").write_text("providers: [openai]\ntheme: dark\n")
    # The branch-unresolvable hint text below assumes this fixture is not inside a
    # work tree; a --basetemp under the checkout would resolve a branch instead.
    assert checked_out_branch(proj) is None
    return proj


def test_old_project_file_raises_instead_of_loading_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    proj = _old_name_repo(tmp_path)
    with pytest.raises(LoadError) as exc:
        load_effective_config(control_root=proj)
    # No SourceLoc: str(LoadError) would otherwise prefix a `file:line` naming a
    # path that does not exist. The message alone carries the instruction.
    # proj is not a git repo, so this is also the branch-unresolvable hint form.
    assert str(exc.value) == (
        f"imp/project.yaml found in {proj} but imp now reads imp/config.yaml — "
        f"rename it with `git -C {proj} mv imp/project.yaml imp/config.yaml`")


def test_new_name_present_wins_and_the_old_file_is_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    proj = _old_name_repo(tmp_path)
    (proj / "imp" / "config.yaml").write_text("theme: light\n")
    eff = load_effective_config(control_root=proj)
    assert eff.config.theme == "light"
    # `providers: [openai]` lives only in the old file; the floor sets no
    # providers, so an empty tuple proves the old file was not merged in.
    assert eff.config.providers == ()


def test_neither_file_present_still_loads_the_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    proj = tmp_path / "empty"
    (proj / "imp").mkdir(parents=True)
    eff = load_effective_config(control_root=proj)
    assert eff.config.theme == "ansi"


def test_preflight_still_swallows_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Spec scope 6: _preflight_imp_init's LoadError swallow is deliberate and
    # unchanged — "run `imp init` first" is the wrong advice for a repo already
    # initialized under the old name, so the guard surfaces from the readers
    # that do not swallow it (Quest.create's own load, and `imp config set`).
    from imp.cli.main import _preflight_imp_init

    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    proj = _old_name_repo(tmp_path)
    assert _preflight_imp_init(control_root=proj, project_root=proj) is None


def test_hint_names_the_branch_the_old_file_sits_on(tmp_path: Path) -> None:
    proj = _old_name_repo(tmp_path)
    # An unborn branch already resolves through `symbolic-ref --short HEAD`, so
    # no commit is needed to pin the branch clause.
    _git(proj, "init", "-q", "-b", "imp/dajo65")
    assert project_rename_hint(proj) == (
        f"imp/project.yaml found in {proj} (branch imp/dajo65) but imp now reads "
        f"imp/config.yaml — rename it with "
        f"`git -C {proj} mv imp/project.yaml imp/config.yaml`")


def test_hint_omits_the_branch_clause_on_detached_head(tmp_path: Path) -> None:
    proj = _old_name_repo(tmp_path)
    _git(proj, "init", "-q", "-b", "main")
    _git(proj, "config", "user.email", "t@t")
    _git(proj, "config", "user.name", "t")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "seed")
    _git(proj, "checkout", "-q", "--detach")
    assert checked_out_branch(proj) is None   # really detached
    assert project_rename_hint(proj) == (
        f"imp/project.yaml found in {proj} but imp now reads imp/config.yaml — "
        f"rename it with `git -C {proj} mv imp/project.yaml imp/config.yaml`")


def test_pending_is_old_present_and_new_absent(tmp_path: Path) -> None:
    proj = _old_name_repo(tmp_path)
    assert project_rename_pending(proj) is True
    (proj / "imp" / "config.yaml").write_text("theme: light\n")
    assert project_rename_pending(proj) is False
    empty = tmp_path / "empty"
    (empty / "imp").mkdir(parents=True)
    assert project_rename_pending(empty) is False


def test_the_startup_migration_reads_the_shared_pending_predicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.cli.config_migrate import migrate_project_config
    from imp.providers import effective

    proj = _old_name_repo(tmp_path)
    seen: list[Path] = []

    def spy(root: Path) -> bool:
        seen.append(root)
        return False

    monkeypatch.setattr(effective, "project_rename_pending", spy)
    # Short-circuiting the shared predicate stops the migration at its
    # pure-filesystem fast path: with its own private copy it would instead walk
    # on and raise PreconditionError, since proj is not a git repository.
    migrate_project_config(proj)
    assert seen[0] == proj
    assert (proj / "imp" / "project.yaml").is_file()

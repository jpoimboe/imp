"""The project layer errors instead of loading empty when a repo is still on the
pre-rename `imp/project.yaml` (spec scope 6). A quest branched before the rename
and resumed after it would otherwise silently lose its whole project config."""
from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.errors import LoadError
from imp.providers.effective import load_effective_config

_RENAME_HINT = (
    "imp/project.yaml found but imp now reads imp/config.yaml — rename it on "
    "this branch with `git mv imp/project.yaml imp/config.yaml`"
)


def _old_name_repo(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "imp").mkdir(parents=True)
    (proj / "imp" / "project.yaml").write_text("providers: [openai]\ntheme: dark\n")
    return proj


def test_old_project_file_raises_instead_of_loading_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    proj = _old_name_repo(tmp_path)
    with pytest.raises(LoadError) as exc:
        load_effective_config(project_root=proj, control_root=None)
    # No SourceLoc: str(LoadError) would otherwise prefix a `file:line` naming a
    # path that does not exist. The message alone carries the instruction.
    assert str(exc.value) == _RENAME_HINT


def test_new_name_present_wins_and_the_old_file_is_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    proj = _old_name_repo(tmp_path)
    (proj / "imp" / "config.yaml").write_text("theme: light\n")
    eff = load_effective_config(project_root=proj, control_root=None)
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
    eff = load_effective_config(project_root=proj, control_root=None)
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

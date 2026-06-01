# tests/test_skills_resolution.py
from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.skills import (
    SkillError,
    SkillNameError,
    SkillNotFound,
    list_skill_names,
    load_skill_body,
)


def _skills(tmp_path: Path) -> Path:
    d = tmp_path / "workflow" / "skills"
    d.mkdir(parents=True)
    (d / "refactor.md").write_text("# Refactor\nbody line\n")
    (d / "debug.md").write_text("# Debug\n")
    return d


def test_load_skill_body_returns_full_markdown(tmp_path: Path) -> None:
    skills = _skills(tmp_path)
    assert load_skill_body(skills, "refactor") == "# Refactor\nbody line\n"


def test_load_skill_body_missing_file_raises_not_found(tmp_path: Path) -> None:
    skills = _skills(tmp_path)
    with pytest.raises(SkillNotFound):
        load_skill_body(skills, "nope")


def test_load_skill_body_missing_dir_raises_not_found_not_oserror(tmp_path: Path) -> None:
    # A Change created before skills/ existed must behave as not-found.
    missing = tmp_path / "workflow" / "skills"  # never created
    with pytest.raises(SkillNotFound):
        load_skill_body(missing, "refactor")


@pytest.mark.parametrize(
    "bad", ["", "   ", "\t", ".", "..", "../config", "a/b", "/etc/passwd", "x\\y"]
)
def test_load_skill_body_rejects_traversal(tmp_path: Path, bad: str) -> None:
    skills = _skills(tmp_path)
    with pytest.raises(SkillNameError):
        load_skill_body(skills, bad)


def test_list_skill_names_sorted_stems(tmp_path: Path) -> None:
    skills = _skills(tmp_path)
    assert list_skill_names(skills) == ["debug", "refactor"]


def test_list_skill_names_missing_dir_is_empty(tmp_path: Path) -> None:
    assert list_skill_names(tmp_path / "workflow" / "skills") == []


def test_skill_errors_share_a_base(tmp_path: Path) -> None:
    # Callers can catch the whole family with one `except SkillError`...
    assert issubclass(SkillNameError, SkillError)
    assert issubclass(SkillNotFound, SkillError)
    # ...while SkillNameError keeps its ValueError identity.
    assert issubclass(SkillNameError, ValueError)
    skills = _skills(tmp_path)
    with pytest.raises(SkillError):
        load_skill_body(skills, "../escape")  # bad name -> SkillNameError
    with pytest.raises(SkillError):
        load_skill_body(skills, "nope")  # missing file -> SkillNotFound

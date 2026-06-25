# tests/test_skills_resolution.py
from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.skills import (
    SkillError,
    SkillNameError,
    SkillNotFound,
    list_user_skills,
    load_agent_skill,
    load_user_skill,
)


def test_skill_errors_share_a_base(tmp_path: Path) -> None:
    assert issubclass(SkillNameError, SkillError)
    assert issubclass(SkillNotFound, SkillError)
    assert issubclass(SkillNameError, ValueError)
    skills = _split(tmp_path)
    with pytest.raises(SkillError):
        load_user_skill(skills, "../escape")   # bad name -> SkillNameError
    with pytest.raises(SkillError):
        load_user_skill(skills, "nope")        # missing file -> SkillNotFound


def _split(tmp_path: Path) -> Path:
    # local helper: build skills/agent and skills/user trees
    skills = tmp_path / "workflow" / "skills"
    (skills / "agent").mkdir(parents=True)
    (skills / "user").mkdir(parents=True)
    return skills


def test_load_agent_skill_resolves_agent_dir(tmp_path: Path) -> None:
    skills = _split(tmp_path)
    (skills / "agent" / "tdd.md").write_text("# TDD\nred green\n")
    assert load_agent_skill(skills, "tdd") == "# TDD\nred green\n"


def test_load_agent_skill_falls_back_to_user_dir(tmp_path: Path) -> None:
    skills = _split(tmp_path)
    (skills / "user" / "shared.md").write_text("# Shared\nuser body\n")
    # No agent/shared.md exists, so the agent path resolves it via user fallback.
    assert load_agent_skill(skills, "shared") == "# Shared\nuser body\n"


def test_load_agent_skill_strips_frontmatter_on_user_fallback(tmp_path: Path) -> None:
    skills = _split(tmp_path)
    (skills / "user" / "shared.md").write_text(
        "---\ndescription: A shared skill\n---\n# Shared\nbody\n"
    )
    assert load_agent_skill(skills, "shared") == "# Shared\nbody\n"


def test_load_agent_skill_missing_everywhere_raises_not_found(tmp_path: Path) -> None:
    skills = _split(tmp_path)
    with pytest.raises(SkillNotFound):
        load_agent_skill(skills, "ghost")


def test_load_agent_skill_missing_subdirs_read_as_not_found(tmp_path: Path) -> None:
    # Neither agent/ nor user/ exists: not an IO error, a clean not-found.
    skills = tmp_path / "workflow" / "skills"
    skills.mkdir(parents=True)
    with pytest.raises(SkillNotFound):
        load_agent_skill(skills, "tdd")


def test_load_user_skill_reads_user_only(tmp_path: Path) -> None:
    skills = _split(tmp_path)
    (skills / "agent" / "secret.md").write_text("# Secret\nagent only\n")
    # The human path never sees agent disciplines, even by exact name.
    with pytest.raises(SkillNotFound):
        load_user_skill(skills, "secret")


def test_load_user_skill_strips_frontmatter(tmp_path: Path) -> None:
    skills = _split(tmp_path)
    (skills / "user" / "refactor.md").write_text(
        "---\ndescription: Refactor helper\n---\n# Refactor\nstep one\n"
    )
    assert load_user_skill(skills, "refactor") == "# Refactor\nstep one\n"


def test_load_user_skill_no_frontmatter_returns_body_unchanged(tmp_path: Path) -> None:
    skills = _split(tmp_path)
    (skills / "user" / "plain.md").write_text("# Plain\njust body\n")
    assert load_user_skill(skills, "plain") == "# Plain\njust body\n"


@pytest.mark.parametrize("bad", ["../agent/secret", "..", "a/b", "/etc/passwd"])
def test_load_user_skill_rejects_traversal(tmp_path: Path, bad: str) -> None:
    skills = _split(tmp_path)
    (skills / "agent" / "secret.md").write_text("# Secret\n")
    with pytest.raises(SkillNameError):
        load_user_skill(skills, bad)


def test_list_user_skills_returns_stem_and_description(tmp_path: Path) -> None:
    skills = _split(tmp_path)
    (skills / "user" / "refactor.md").write_text(
        "---\ndescription: Refactor helper\n---\n# Refactor\n"
    )
    (skills / "user" / "plain.md").write_text("# Plain\n")
    # agent skills never appear in the human listing
    (skills / "agent" / "tdd.md").write_text("# TDD\n")
    assert list_user_skills(skills) == [
        ("plain", None),
        ("refactor", "Refactor helper"),
    ]


def test_list_user_skills_missing_user_dir_is_empty(tmp_path: Path) -> None:
    skills = tmp_path / "workflow" / "skills"
    skills.mkdir(parents=True)
    assert list_user_skills(skills) == []


def test_load_agent_skill_strips_frontmatter_in_agent_dir(tmp_path: Path) -> None:
    skills = _split(tmp_path)
    (skills / "agent" / "tdd.md").write_text(
        "---\ndescription: TDD discipline\n---\n# TDD\nred green\n"
    )
    assert load_agent_skill(skills, "tdd") == "# TDD\nred green\n"


def test_load_user_skill_unterminated_frontmatter_returned_unchanged(tmp_path: Path) -> None:
    skills = _split(tmp_path)
    raw = "---\ndescription: foo\n# Body never closes the block\n"
    (skills / "user" / "weird.md").write_text(raw)
    assert load_user_skill(skills, "weird") == raw


def test_load_agent_skill_rejects_traversal(tmp_path: Path) -> None:
    skills = _split(tmp_path)
    with pytest.raises(SkillNameError):
        load_agent_skill(skills, "../user/secret")

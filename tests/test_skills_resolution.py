# tests/test_skills_resolution.py
from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.skills import (
    SkillError,
    SkillNameError,
    SkillNotFound,
    list_internal_skills,
    list_user_skills,
    load_internal_skill,
    load_user_skill,
    overlay_global_skills,
    overlay_project_skills,
)
from imp.assets import bundled_skills_dir

_POOL = (
    "imp-tool-reference",
    "imp-driving-terminal-uis",
    "imp-driving-web-uis",
    "imp-knowledge",
)


def _skills(tmp_path: Path) -> Path:
    # local helper: build a skills/ root with a user/ subdir
    skills = tmp_path / "workflow" / "skills"
    skills.mkdir(parents=True)
    (skills / "user").mkdir()
    return skills


def test_skill_errors_share_a_base(tmp_path: Path) -> None:
    assert issubclass(SkillNameError, SkillError)
    assert issubclass(SkillNotFound, SkillError)
    assert issubclass(SkillNameError, ValueError)
    skills = _skills(tmp_path)
    with pytest.raises(SkillError):
        load_user_skill(skills, "../escape")   # bad name -> SkillNameError
    with pytest.raises(SkillError):
        load_user_skill(skills, "nope")        # missing file -> SkillNotFound


def test_load_internal_skill_resolves_root(tmp_path: Path) -> None:
    skills = _skills(tmp_path)
    (skills / "tdd.md").write_text("# TDD\nred green\n")
    assert load_internal_skill(skills, "tdd") == "# TDD\nred green\n"


def test_load_internal_skill_falls_back_to_user_dir(tmp_path: Path) -> None:
    skills = _skills(tmp_path)
    (skills / "user" / "shared.md").write_text("# Shared\nuser body\n")
    # No root shared.md exists, so it resolves via the user/ fallback.
    assert load_internal_skill(skills, "shared") == "# Shared\nuser body\n"


def test_load_internal_skill_root_shadows_user(tmp_path: Path) -> None:
    skills = _skills(tmp_path)
    (skills / "dup.md").write_text("ROOT\n")
    (skills / "user" / "dup.md").write_text("USER\n")
    # Root is tried first, so the root copy wins.
    assert load_internal_skill(skills, "dup") == "ROOT\n"


@pytest.mark.timeout(240)  # wall-clock SIGALRM can false-fire under -n auto saturation
def test_load_internal_skill_strips_frontmatter_on_user_fallback(tmp_path: Path) -> None:
    skills = _skills(tmp_path)
    (skills / "user" / "shared.md").write_text(
        "---\ndescription: A shared skill\n---\n# Shared\nbody\n"
    )
    assert load_internal_skill(skills, "shared") == "# Shared\nbody\n"


def test_load_internal_skill_strips_frontmatter_in_root(tmp_path: Path) -> None:
    skills = _skills(tmp_path)
    (skills / "tdd.md").write_text(
        "---\ndescription: TDD discipline\n---\n# TDD\nred green\n"
    )
    assert load_internal_skill(skills, "tdd") == "# TDD\nred green\n"


def test_load_internal_skill_missing_everywhere_raises_not_found(tmp_path: Path) -> None:
    skills = _skills(tmp_path)
    with pytest.raises(SkillNotFound):
        load_internal_skill(skills, "ghost")


def test_load_internal_skill_missing_user_dir_reads_as_not_found(tmp_path: Path) -> None:
    # A skills/ root with no user/ subdir: not an IO error, a clean not-found.
    skills = tmp_path / "workflow" / "skills"
    skills.mkdir(parents=True)
    with pytest.raises(SkillNotFound):
        load_internal_skill(skills, "tdd")


def test_load_internal_skill_rejects_traversal(tmp_path: Path) -> None:
    skills = _skills(tmp_path)
    with pytest.raises(SkillNameError):
        load_internal_skill(skills, "../user/secret")


def test_load_user_skill_reads_user_only(tmp_path: Path) -> None:
    skills = _skills(tmp_path)
    (skills / "secret.md").write_text("# Secret\ninternal only\n")
    # The human path never sees a root (internal) skill, even by exact name.
    with pytest.raises(SkillNotFound):
        load_user_skill(skills, "secret")


def test_load_user_skill_strips_frontmatter(tmp_path: Path) -> None:
    skills = _skills(tmp_path)
    (skills / "user" / "refactor.md").write_text(
        "---\ndescription: Refactor helper\n---\n# Refactor\nstep one\n"
    )
    assert load_user_skill(skills, "refactor") == "# Refactor\nstep one\n"


def test_load_user_skill_no_frontmatter_returns_body_unchanged(tmp_path: Path) -> None:
    skills = _skills(tmp_path)
    (skills / "user" / "plain.md").write_text("# Plain\njust body\n")
    assert load_user_skill(skills, "plain") == "# Plain\njust body\n"


@pytest.mark.parametrize("bad", ["../secret", "..", "a/b", "/etc/passwd"])
def test_load_user_skill_rejects_traversal(tmp_path: Path, bad: str) -> None:
    skills = _skills(tmp_path)
    (skills / "secret.md").write_text("# Secret\n")
    with pytest.raises(SkillNameError):
        load_user_skill(skills, bad)


def test_load_user_skill_unterminated_frontmatter_returned_unchanged(tmp_path: Path) -> None:
    skills = _skills(tmp_path)
    raw = "---\ndescription: foo\n# Body never closes the block\n"
    (skills / "user" / "weird.md").write_text(raw)
    assert load_user_skill(skills, "weird") == raw


def test_list_user_skills_returns_stem_and_description(tmp_path: Path) -> None:
    skills = _skills(tmp_path)
    (skills / "user" / "refactor.md").write_text(
        "---\ndescription: Refactor helper\n---\n# Refactor\n"
    )
    (skills / "user" / "plain.md").write_text("# Plain\n")
    # internal (root) skills never appear in the human listing
    (skills / "tdd.md").write_text("# TDD\n")
    assert list_user_skills(skills) == [
        ("plain", None),
        ("refactor", "Refactor helper"),
    ]


def test_list_user_skills_missing_user_dir_is_empty(tmp_path: Path) -> None:
    skills = tmp_path / "workflow" / "skills"
    skills.mkdir(parents=True)
    assert list_user_skills(skills) == []


def test_list_internal_skills_top_level_only(tmp_path: Path) -> None:
    skills = _skills(tmp_path)
    (skills / "b.md").write_text("# B\n")
    (skills / "a.md").write_text("# A\n")
    (skills / "notes.txt").write_text("not a skill\n")
    (skills / "user" / "mine.md").write_text("# Mine\n")
    assert list_internal_skills(skills) == ["a", "b"]


def test_list_internal_skills_missing_dir_is_empty(tmp_path: Path) -> None:
    assert list_internal_skills(tmp_path / "nope") == []


def test_overlay_copies_project_skills_into_user_dir(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / "imp" / "skills").mkdir(parents=True)
    (project / "imp" / "skills" / "mine.md").write_text("# Mine\nproject body\n")
    dest = tmp_path / "snap" / "skills" / "user"
    overlay_project_skills(project, dest)
    assert (dest / "mine.md").read_text() == "# Mine\nproject body\n"


def test_overlay_shadows_workflow_user_skill_on_collision(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / "imp" / "skills").mkdir(parents=True)
    (project / "imp" / "skills" / "refactor.md").write_text("PROJECT\n")
    dest = tmp_path / "snap" / "skills" / "user"
    dest.mkdir(parents=True)
    (dest / "refactor.md").write_text("WORKFLOW\n")
    overlay_project_skills(project, dest)
    assert (dest / "refactor.md").read_text() == "PROJECT\n"


def test_overlay_missing_source_is_noop(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    dest = tmp_path / "snap" / "skills" / "user"
    overlay_project_skills(project, dest)  # must not raise
    assert not dest.exists()


def test_bundled_skills_dir_holds_the_global_pool() -> None:
    pool = bundled_skills_dir()
    assert pool.is_dir()
    for name in _POOL:
        assert (pool / f"{name}.md").is_file(), name


def test_overlay_global_lands_the_pool_into_dest_root(tmp_path: Path) -> None:
    dest = tmp_path / "snap" / "skills"
    overlay_global_skills(dest)  # dest does not pre-exist: overlay must create it
    for name in _POOL:
        body = (dest / f"{name}.md").read_text()
        assert body.strip(), name
    # The pool copy is byte-identical to the shipped source.
    assert (dest / "imp-knowledge.md").read_text() == (
        bundled_skills_dir() / "imp-knowledge.md"
    ).read_text()


def test_overlay_global_does_not_clobber_a_workflow_skill(tmp_path: Path) -> None:
    dest = tmp_path / "snap" / "skills"
    dest.mkdir(parents=True)
    (dest / "imp-knowledge.md").write_text("WORKFLOW OWN\n")
    overlay_global_skills(dest)
    # Workflow-wins: an already-present same-named file is left untouched.
    assert (dest / "imp-knowledge.md").read_text() == "WORKFLOW OWN\n"
    # …while the other pool skills are still overlaid alongside it.
    assert (dest / "imp-tool-reference.md").read_text().strip()


def test_overlay_global_makes_the_web_ui_skill_loadable_by_name(tmp_path: Path) -> None:
    dest = tmp_path / "snap" / "skills"
    overlay_global_skills(dest)
    # An agent's load_skill(name="imp-driving-web-uis") resolves against a snapshot's
    # skills/ root and gets the shipped bytes back.
    assert load_internal_skill(dest, "imp-driving-web-uis") == (
        bundled_skills_dir() / "imp-driving-web-uis.md"
    ).read_text()


def test_web_ui_skill_teaches_the_headless_render() -> None:
    body = load_internal_skill(bundled_skills_dir(), "imp-driving-web-uis")
    assert body.startswith("# Driving web UIs\n")
    for anchor in (
        "--headless=new",
        "--no-sandbox",
        "--screenshot",
        "firefox --headless",
        "~/.cache/ms-playwright/",
        "`read_file` the PNG",
        "fall back to drawing the page in ASCII",
    ):
        assert anchor in body, anchor

"""The shipped default workflow ships the four discipline skills (spec §7),
loadable via the skills mechanism, with no YAML frontmatter (Profile B)."""

from __future__ import annotations

from imp.agents.skills import list_skill_names, load_skill_body
from imp.assets import default_workflow_dir

_SKILLS = [
    "test-driven-development",
    "systematic-debugging",
    "verification-before-completion",
    "receiving-code-review",
]


def _skills_dir():
    return default_workflow_dir() / "skills"


def test_all_four_skills_present() -> None:
    assert set(_SKILLS).issubset(set(list_skill_names(_skills_dir())))


def test_skills_load_and_have_no_frontmatter() -> None:
    for name in _SKILLS:
        body = load_skill_body(_skills_dir(), name)
        assert body.strip(), f"{name} is empty"
        # Profile B: flat markdown, no YAML frontmatter delimiter.
        assert not body.lstrip().startswith("---"), f"{name} has frontmatter"


def test_skills_are_imp_native_not_superpowers() -> None:
    # Porting fidelity (spec §8/§14): harness scaffolding stripped.
    for name in _SKILLS:
        body = load_skill_body(_skills_dir(), name)
        assert "superpowers:" not in body, f"{name} keeps a superpowers: cross-ref"
        assert "Skill tool" not in body, f"{name} keeps a Skill-tool reference"

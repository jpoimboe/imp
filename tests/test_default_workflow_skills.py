"""The shipped default workflow ships its discipline skills under skills/agent/,
loadable via the agent path with no YAML frontmatter; skills/user/ ships empty.
This module covers the four core disciplines; the two imp-* grounding skills are
covered in test_imp_knowledge_skills.py."""

from __future__ import annotations

from imp.agents.skills import list_user_skills, load_agent_skill
from imp.assets import bundled_workflow_dir

_SKILLS = [
    "test-driven-development",
    "systematic-debugging",
    "verification-before-completion",
    "receiving-code-review",
]


def _skills_dir():
    return bundled_workflow_dir("imp") / "skills"


def test_all_four_skills_present_via_agent_path() -> None:
    for name in _SKILLS:
        assert load_agent_skill(_skills_dir(), name).strip(), f"{name} missing/empty"


def test_user_skills_ship_empty() -> None:
    assert list_user_skills(_skills_dir()) == []


def test_skills_load_have_no_frontmatter_and_are_byte_identical() -> None:
    for name in _SKILLS:
        body = load_agent_skill(_skills_dir(), name)
        assert not body.lstrip().startswith("---"), f"{name} has frontmatter"
        # Agent skills carry no frontmatter, so the loader returns them verbatim.
        raw = (_skills_dir() / "agent" / f"{name}.md").read_text()
        assert body == raw, f"{name} body changed on load"


def test_yagni_scope_excludes_cheap_defensive_guards() -> None:
    body = load_agent_skill(_skills_dir(), "receiving-code-review").lower()
    pos = body.find("yagni check")
    assert pos >= 0
    section = body[pos : pos + 900]
    assert "speculativ" in section
    assert "guard" in section
    assert "reachable" in section

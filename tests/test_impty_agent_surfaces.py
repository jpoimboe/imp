"""The two surfaces an agent reads BEFORE it picks a `hard`: the thin one-liner both
provider hosts show for each impty tool, and the imp-driving-terminal-uis skill."""

from __future__ import annotations

from imp.agents.skills import load_internal_skill
from imp.assets import bundled_skills_dir
from impty.mcp import THIN_DESCRIPTIONS
from impty.session import MAX_CALL_HARD


def test_wait_one_liner_states_the_per_call_cap_and_the_resume_instruction() -> None:
    # The full driver docstring is agent-facing only on the standalone stdio
    # server, so this one-liner is all the agent sees under both providers.
    desc = THIN_DESCRIPTIONS["wait"]
    assert f"{MAX_CALL_HARD:g}s" in desc
    assert "call again" in desc


def test_terminal_ui_skill_wait_bullet_states_the_clamp() -> None:
    body = load_internal_skill(bundled_skills_dir(), "imp-driving-terminal-uis")
    # Slicing the bullet proves the sentence sits on `wait`, not merely somewhere
    # in the skill.
    bullet = next((b for b in body.split("\n- ") if b.startswith("**`wait(")), None)
    assert bullet is not None, "the skill has no `wait(` bullet"
    assert f"{MAX_CALL_HARD:g}s" in bullet
    assert "call again" in bullet

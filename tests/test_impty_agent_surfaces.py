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


def test_terminal_ui_skill_closes_by_telling_the_agent_to_paste_the_screen() -> None:
    body = load_internal_skill(bundled_skills_dir(), "imp-driving-terminal-uis")
    parts = body.split("## Showing the human", 1)
    assert len(parts) == 2, "the skill has no `Showing the human` section"
    # Slicing the section proves the rule sits under its own heading, and the
    # end-anchor proves it is the closing section the spec asks for.
    section = " ".join(parts[1].split())
    assert section.startswith(
        "Paste the screen into your message as a fenced block: color survives there."
    )
    # An agent cannot type ESC, so the bare spelling is the load-bearing instruction:
    # without it the skill describes a feature no model can reach.
    assert "write each SGR code without it — `[1;33m`" in section
    # The capability itself: without this sentence the skill still tells the agent
    # nothing outside a fence is restored, and no model would try an inline sample.
    assert ("imp restores it inside the fence, and inside a single-line inline code "
            "span, so a short sample like `` `[1;33m78%[0m` `` keeps its color "
            "mid-sentence") in section
    assert "Outside a fence nothing is restored" not in section
    assert "any control byte that isn't SGR arrives as literal `^[[33m`" in section
    assert " ".join(body.split()).endswith("give the snapshot PNG path too.")


def test_terminal_ui_skill_view_bullet_describes_the_exit_status() -> None:
    body = load_internal_skill(bundled_skills_dir(), "imp-driving-terminal-uis")
    bullet = next((b for b in body.split("\n- ") if b.startswith("**`view(")), None)
    assert bullet is not None, "the skill has no `view(` bullet"
    assert "exit=0" in bullet
    assert "exit=SIGINT" in bullet


def test_stop_one_liner_states_the_graceful_ladder() -> None:
    desc = THIN_DESCRIPTIONS["stop"]
    assert "gracefully" in desc
    assert "SIGTERM" in desc and "SIGKILL" in desc


def test_terminal_ui_skill_stop_bullet_describes_the_ladder_and_the_return() -> None:
    body = load_internal_skill(bundled_skills_dir(), "imp-driving-terminal-uis")
    bullet = next((b for b in body.split("\n- ") if b.startswith("**`stop(")), None)
    assert bullet is not None, "the skill has no `stop(` bullet"
    assert "SIGTERM" in bullet and "SIGKILL" in bullet
    assert "exit status and final screen" in bullet

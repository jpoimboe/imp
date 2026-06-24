"""The front-door persona names both grounding skills under its concierge and
scout hats and carries the load-or-introspect-first grounding rule (spec AC 4)."""

from __future__ import annotations

from imp.assets import bundled_workflow_dir
from imp.engine.loader import load_workflow


def _front_door() -> str:
    wf = load_workflow(bundled_workflow_dir("imp"))
    return wf.agents["front-door"].prompt_template_source


def test_front_door_names_both_skills() -> None:
    body = _front_door()
    assert 'load_skill(name="imp-mental-model")' in body
    assert 'load_skill(name="imp-introspect")' in body


def test_front_door_skills_named_under_concierge_and_scout_hats() -> None:
    body = _front_door()
    concierge = body[body.index("**concierge**"):body.index("**scout**")]
    assert "imp-mental-model" in concierge
    assert "imp-introspect" in concierge
    scout = body[body.index("**scout**"):body.index("**brainstorming")]
    assert "imp-introspect" in scout


def test_front_door_has_grounding_rule_forbidding_memory_answers() -> None:
    body = _front_door()
    assert "## Grounding" in body
    low = body.lower()
    assert "do not answer" in low
    assert "from memory" in low
    grounding = body[body.index("## Grounding"):]
    assert "imp-mental-model" in grounding
    assert "imp-introspect" in grounding


def test_front_door_action_rule_present() -> None:
    body = _front_door()
    assert "## Acting on imp" in body
    # current-session mutations handed back via the slash commands
    assert "/save" in body
    assert "/abandon" in body
    assert "/clear" in body
    low = body.lower()
    # mutations proposed and confirmed first; other-change mutations on confirmation
    assert "confirm" in low


def test_front_door_preserves_speak_plainly() -> None:
    body = _front_door()
    assert "## Speak plainly" in body
    assert "never surface these internal terms" in body


def test_front_door_introduces_no_new_cli_command_or_read_tool() -> None:
    body = _front_door()
    for banned in ("list_changes", "show_change",
                   "imp explain", "imp doctor", "imp workflow show"):
        assert banned not in body, banned

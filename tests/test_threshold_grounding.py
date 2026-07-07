"""The threshold persona names both grounding skills under its concierge and
scout hats and carries the load-or-introspect-first grounding rule (spec AC 4)."""

from __future__ import annotations

from imp.assets import bundled_workflow_dir
from imp.engine.loader import load_workflow


def _threshold() -> str:
    wf = load_workflow(bundled_workflow_dir("imp"))
    return wf.agents["threshold"].prompt_template_source


def test_threshold_names_both_skills() -> None:
    body = _threshold()
    assert 'load_skill(name="imp-mental-model")' in body
    assert 'load_skill(name="imp-introspect")' in body


def test_threshold_skills_named_under_concierge_and_scout_hats() -> None:
    body = _threshold()
    concierge = body[body.index("**concierge**"):body.index("**scout**")]
    assert "imp-mental-model" in concierge
    assert "imp-introspect" in concierge
    scout = body[body.index("**scout**"):body.index("**brainstorming")]
    assert "imp-introspect" in scout


def test_threshold_has_grounding_rule_forbidding_memory_answers() -> None:
    body = _threshold()
    assert "## Grounding" in body
    low = body.lower()
    assert "do not answer" in low
    assert "from memory" in low
    grounding = body[body.index("## Grounding"):]
    assert "imp-mental-model" in grounding
    assert "imp-introspect" in grounding


def test_threshold_action_rule_present() -> None:
    body = _threshold()
    assert "## Acting on imp" in body
    # current-session mutations handed back via the slash commands
    assert "/save" in body
    assert "/abandon" in body
    assert "/clear" in body
    low = body.lower()
    # mutations proposed and confirmed first; other-change mutations on confirmation
    assert "confirm" in low


def test_threshold_preserves_speak_plainly() -> None:
    body = _threshold()
    assert "## Speak plainly" in body
    assert "never surface these internal terms" in body


def test_threshold_introduces_no_new_cli_command_or_read_tool() -> None:
    body = _threshold()
    for banned in ("list_changes", "show_change",
                   "imp explain", "imp doctor", "imp workflow show"):
        assert banned not in body, banned


def test_threshold_has_lazy_orientation_cue() -> None:
    body = _threshold()
    # The cue lives in its own section.
    assert "## Orienting before you offer" in body
    low = body.lower()
    # Orient by reading the tree (manifest/build files) or dispatching an explorer.
    assert "manifest" in low
    assert "explorer" in low
    # Ground the offer in an established stack when one is found.
    assert "established stack" in low
    # Treat an empty/greenfield repo's stack as an open design question.
    assert "greenfield" in low
    assert "open design question" in low
    # Orientation is lazy / on demand — a pure imp-mechanics question does not
    # trigger a project-stack read.
    assert "how does imp work" in low
    assert "lazy" in low or "on demand" in low
    # The cue honors Speak plainly and surfaces no internal terms. Scope this to
    # the new section so the assertion can't pass on the body's pre-existing
    # "## Speak plainly" heading.
    marker = "## Orienting before you offer"
    start = body.index(marker)
    section = body[start:body.index("{% include", start)].lower()
    assert "speak plainly" in section

from __future__ import annotations

from pathlib import Path

from imp.engine.agent_run import _CORE_TAIL

_AGENTS_DIR = Path("src/imp/assets/workflows/imp/agents")
_PARTIAL = Path("src/imp/assets/partials/action-care.md")
_CODING_STANCE_PARTIAL = Path("src/imp/assets/partials/coding-stance.md")

_ACTION_CARE_AGENTS = {
    "coder.md",
    "task-lead.md",
    "deferred-sweep.md",
    "spiker.md",
    "autosquash-resolve.md",
    "reconciler.md",
    "plan-writer.md",
    "spec-finalizer.md",
    "finalize.md",
}

_CODING_STANCE_AGENTS = {
    "coder.md",
    "task-lead.md",
    "deferred-sweep.md",
    "spiker.md",
    "autosquash-resolve.md",
    "reconciler.md",
    "code-reviewer.md",
    "batch-code-reviewer.md",
    "gap-check.md",
    "integration-review.md",
    "plan-writer.md",
    "plan-reviewer.md",
    "spec-finalizer.md",
    "spec-reviewer.md",
    "finalize.md",
}


def test_action_care_not_in_core_tail() -> None:
    assert "Measure twice, cut once" not in _CORE_TAIL


def test_action_care_partial_exists_and_contains_text() -> None:
    assert _PARTIAL.exists(), f"partial not found: {_PARTIAL}"
    assert "Measure twice, cut once" in _PARTIAL.read_text()


def test_action_care_include_in_exactly_the_right_agents() -> None:
    include = '{% include "partials/action-care.md" %}'
    for path in _AGENTS_DIR.glob("*.md"):
        text = path.read_text()
        if path.name in _ACTION_CARE_AGENTS:
            assert include in text, f"missing include in {path.name}"
        else:
            assert include not in text, f"unexpected include in {path.name}"


def test_coding_stance_partial_exists_with_expected_anchors() -> None:
    assert _CODING_STANCE_PARTIAL.exists(), \
        f"partial not found: {_CODING_STANCE_PARTIAL}"
    text = _CODING_STANCE_PARTIAL.read_text()
    assert "backwards-compatibility hacks" in text
    assert "YAGNI ruthlessly" in text


def test_coding_stance_include_in_exactly_the_right_agents() -> None:
    include = '{% include "partials/coding-stance.md" %}'
    for path in _AGENTS_DIR.glob("*.md"):
        text = path.read_text()
        if path.name in _CODING_STANCE_AGENTS:
            assert include in text, \
                f"missing coding-stance include in {path.name}"
        else:
            assert include not in text, \
                f"unexpected coding-stance include in {path.name}"


def test_action_care_renders_into_agent_prompt() -> None:
    from imp.agents.prompts import build_context, render_prompt
    from imp.engine.loader import parse_agent_file
    from imp.engine.state import InMemoryEventLog

    agent = parse_agent_file(_AGENTS_DIR / "coder.md")
    wf = _AGENTS_DIR.parent  # workflow_dir so {% include %} resolves
    rendered = render_prompt(
        agent.prompt_template_source,
        build_context(quest_title="t", event_log=InMemoryEventLog(),
                      vars={"fast_path": False}),
        workflow_dir=wf)
    assert "Measure twice, cut once" in rendered


def test_front_door_includes_coding_stance_and_action_care() -> None:
    fd = Path("src/imp/assets/partials/front-door.md").read_text()
    assert '{% include "partials/coding-stance.md" %}' in fd
    assert '{% include "partials/action-care.md" %}' in fd

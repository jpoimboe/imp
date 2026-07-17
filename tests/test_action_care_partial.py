from __future__ import annotations

from pathlib import Path

from imp.engine.agent_run import _CORE_TAIL

_AGENTS_DIR = Path("src/imp/assets/workflows/imp/agents")
_PARTIAL = Path("src/imp/assets/workflows/imp/partials/action-care.md")

_ACTION_CARE_AGENTS = {
    "coder.md",
    "task-lead.md",
    "deferred-sweep.md",
    "spiker.md",
    "autosquash-resolve.md",
    "reconciler.md",
    "plan-writer.md",
    "spec-finalizer.md",
    "threshold.md",
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

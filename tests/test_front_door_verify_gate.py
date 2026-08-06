"""The front-door partial's unwired-gate block: verbatim, positioned with the
orientation guidance, and conditional on project.verify_gate_unset at both front
doors (default/threshold and minimal/front-door)."""

from __future__ import annotations

from pathlib import Path

from imp.agents.prompts import build_context, render_prompt
from imp.assets import bundled_workflow_dir
from imp.engine.env import TestGates
from imp.engine.loader import load_workflow
from imp.engine.state import InMemoryEventLog

FRONT_DOOR = Path("src/imp/assets/partials/front-door.md")

BLOCK = """{% if project.verify_gate_unset %}
**No test gate is wired here.** `test_cmd_fast` is unset, so the check imp runs
after each round of coding is a no-op and every acceptance criterion you write
goes unenforced. If what you scope introduces or already has a test suite, fold
wiring it into this quest: pin the exact command in the spec (e.g.
`ruff check . && pytest -q`) for the implementer to set in `imp/config.yaml`
alongside the code. Raise it once — if the project runs no tests by choice, let
it go.
{% endif %}"""

MARKER = "**No test gate is wired here.**"


def _render(workflow: str, agent: str, fast: str | None) -> str:
    wf = load_workflow(bundled_workflow_dir(workflow))
    return render_prompt(
        wf.agents[agent].prompt_template_source,
        build_context(quest_title="t", event_log=InMemoryEventLog(),
                      verify_gate_unset=TestGates(fast=fast).fast_unset),
        workflow_dir=bundled_workflow_dir(workflow),
    )


def test_block_is_verbatim_and_sits_with_the_orientation_guidance() -> None:
    fd = FRONT_DOOR.read_text()
    assert BLOCK in fd
    lazy = fd.index("Orientation is lazy — on demand, not reflexive.")
    principles = fd.index('{% include "partials/spec-design-principles.md" %}')
    assert lazy < fd.index(BLOCK) < principles


def test_threshold_speaks_only_when_the_fast_gate_is_unwired() -> None:
    # None = never wired (speak); a command = wired; "" = deliberate opt-out (stay quiet).
    assert MARKER in _render("default", "threshold", None)
    assert MARKER not in _render("default", "threshold", "make check")
    assert MARKER not in _render("default", "threshold", "")


def test_minimal_front_door_speaks_when_the_fast_gate_is_unwired() -> None:
    assert MARKER in _render("minimal", "front-door", None)

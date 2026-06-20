"""The {{ project.guidance }} slot lands in judgment-making prompts only, and
renders the maintainer's directive when set (spec item 5 / AC 9)."""
from __future__ import annotations

from pathlib import Path

from imp.agents.prompts import build_context, render_prompt
from imp.engine.state import InMemoryEventLog

AGENTS = Path("src/imp/assets/workflows/imp/agents")
SCRIPTS = Path("src/imp/assets/workflows/imp/scripts")

TARGETS = [
    "spec-writer", "plan-writer", "implementer",
    "spec-reviewer", "plan-reviewer", "code-reviewer", "quality-reviewer",
]
NON_TARGETS = [
    "task-lead", "integrator", "deferred-sweep", "plan-cycle-check",
    "finalize", "autosquash-resolve", "explorer", "researcher",
]

GUIDANCE_BLOCK = (
    "{% if project.guidance %}\n"
    "## Project guidance\n\n"
    "The project maintainer set this guidance for this codebase — follow it "
    "unless it conflicts with a hard correctness or safety requirement:\n\n"
    "{{ project.guidance }}\n\n"
    "{% endif %}\n"
)


def test_slot_present_in_every_judgment_prompt() -> None:
    for stem in TARGETS:
        assert GUIDANCE_BLOCK in (AGENTS / f"{stem}.md").read_text(), stem


def test_slot_absent_from_mechanical_and_orchestration_prompts() -> None:
    for stem in NON_TARGETS:
        assert "{{ project.guidance }}" not in (AGENTS / f"{stem}.md").read_text(), stem


def test_slot_absent_from_scripts() -> None:
    for sh in SCRIPTS.glob("*"):
        if sh.is_file():
            assert "{{ project.guidance }}" not in sh.read_text(), sh.name


def test_block_renders_directive_when_guidance_set() -> None:
    ctx = build_context("t", InMemoryEventLog(), guidance="NO BACKCOMPAT.")
    out = render_prompt(GUIDANCE_BLOCK, ctx)
    assert "## Project guidance" in out
    assert "NO BACKCOMPAT." in out


def test_block_is_empty_when_guidance_unset() -> None:
    ctx = build_context("t", InMemoryEventLog(), guidance="")
    out = render_prompt(GUIDANCE_BLOCK, ctx)
    assert "## Project guidance" not in out
    assert out.strip() == ""

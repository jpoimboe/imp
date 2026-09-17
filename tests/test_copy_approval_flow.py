from __future__ import annotations

from pathlib import Path

WF = Path("src/imp/assets/workflows/default")
PARTIALS = Path("src/imp/assets/partials")
AGENTS = WF / "agents"


def test_brainstorm_dialogue_drops_copy_from_enumerate_list_and_adds_copy_paragraph() -> None:
    text = (PARTIALS / "brainstorm-dialogue.md").read_text()
    # 1a: `copy` no longer sits in the Enumerate-before-committing element list.
    assert "copy, colors, visibility, layout" not in text
    assert "labels,\ncolors, visibility, layout — must have" in text
    assert (
        "colors, visibility, layout — must have its concrete values enumerated to the user"
        in text
    )
    # 1b: the dedicated copy paragraph, verbatim.
    assert (
        "> **Enumerate copy verbatim.** Every fixed string — anything a human will "
        "see (labels, messages, error/diagnostic/log output) and any LLM prompt text "
        "— has its exact wording shown to the user before the spec is written, not "
        "just its intent."
        in text
    )
    # Placement: after Enumerate before committing, before Walk the whole surface.
    idx_before = text.index("**Enumerate before committing.**")
    idx_copy = text.index("**Enumerate copy verbatim.**")
    idx_walk = text.index("**Walk the whole surface.**")
    assert idx_before < idx_copy < idx_walk


def test_spec_finalizer_carries_unenumerated_copy_backstop() -> None:
    text = (AGENTS / "spec-finalizer.md").read_text()
    assert (
        "> **Unenumerated copy is a non-blocking case of this.** If the design adds "
        "new user-facing copy — any string a human sees, or LLM prompt text — whose "
        "exact wording the threshold interview never enumerated, do not gate landing "
        "on it. Pin a reasonable default as spec content, styled after neighbouring "
        "copy, and record the wording as one `— needs-decision:` item so it rides to "
        "the merge gate. Never write \"approval-gated before it lands\" or anything "
        "that stalls the headless plan phase."
        in text
    )
    # Placement: closes the Surfacing-a-decision section, before Pinning complexity.
    idx_para = text.index("**Unenumerated copy is a non-blocking case of this.**")
    idx_section = text.index("## Surfacing a decision you cannot resolve")
    idx_next = text.index("## Pinning complexity")
    assert idx_section < idx_para < idx_next


def test_plan_writer_carries_copy_decide_and_defer_guard() -> None:
    text = (AGENTS / "plan-writer.md").read_text()
    assert (
        "New user-facing copy — any string a human sees, or LLM prompt text — whose "
        "wording the spec left unpinned is this same decide-and-record case: "
        "implement a sensible default styled after neighbouring copy and ensure one "
        "`— needs-decision:` line carries the wording to the gate — never stop "
        "mid-plan for copy."
        in text
    )
    # Placement: closes the Surfacing-a-decision section, before Explore the Codebase.
    idx_para = text.index("New user-facing copy — any string a human sees")
    idx_section = text.index("## Surfacing a decision you cannot resolve")
    idx_next = text.index("## Explore the Codebase First")
    assert idx_section < idx_para < idx_next

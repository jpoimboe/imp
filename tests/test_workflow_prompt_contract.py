from __future__ import annotations

from pathlib import Path

import pytest

AGENTS_DIR = Path("src/imp/assets/workflows/default/agents")

# Subagents (non-interactive, dispatched via `task`): the contract under test.
SUBAGENT_FILES = ["implementer.md", "spec-task-reviewer.md", "quality-reviewer.md"]
# Interactive STEPS: the executor wires their resume hooks, so a turn-ending
# question is correct for them. Exempt from the no-bare-question rule.
INTERACTIVE_STEP_FILES = ["spec-writer.md", "final-review.md"]

SKILLS_DIR = Path("src/imp/assets/workflows/default/skills")

# Falsifiability-hardening guards: each component's edit leaves a marker in a
# specific file. These are per-(path, marker) pairs spanning skills/ and direct
# agents/ reads (including quality-reviewer, already in SUBAGENT_FILES for the
# complete_step guards — reused here for a *different* marker, not re-listed for
# the same purpose). NOT a uniform sweep over SUBAGENT_FILES. A presence guard
# proves the marker exists somewhere in the file; section placement is left to
# author self-review and the reviewer pass.
FALSIFIABILITY_MARKERS = [
    (AGENTS_DIR / "code-reviewer.md", "falsifiab"),     # C: whole-change backstop
    (AGENTS_DIR / "quality-reviewer.md", "falsifiab"),  # C: per-task tripwire
]


@pytest.mark.parametrize(
    "path,marker",
    FALSIFIABILITY_MARKERS,
    ids=[f"{p.name}:{m}" for p, m in FALSIFIABILITY_MARKERS],
)
def test_falsifiability_marker_present(path: Path, marker: str) -> None:
    assert marker.lower() in path.read_text().lower(), (
        f"{path.name}: missing falsifiability marker {marker!r} — the "
        f"test-falsifiability hardening edit for this file is absent")


def _read(name: str) -> str:
    return (AGENTS_DIR / name).read_text()


@pytest.mark.parametrize("name", SUBAGENT_FILES)
def test_subagent_prompt_references_complete_step(name: str) -> None:
    assert "complete_step" in _read(name), (
        f"{name}: subagent prompt must name its terminal complete_step contract")


@pytest.mark.parametrize("name", SUBAGENT_FILES)
def test_subagent_prompt_terminal_is_not_report_back_prose(name: str) -> None:
    text = _read(name).lower()
    # The dogfood failure framing: "report back" as the terminal action with no
    # complete_step. We forbid the bare phrase "report back" entirely in
    # subagent prompts now that the contract is explicit.
    assert "report back" not in text, (
        f"{name}: drop 'report back' framing — the terminal action is complete_step")


@pytest.mark.parametrize("name", SUBAGENT_FILES)
def test_subagent_prompt_forbids_turn_ending_question(name: str) -> None:
    text = _read(name).lower()
    for banned in ("end your turn with the question",
                   "ok to pause and clarify"):
        assert banned not in text, (
            f"{name}: subagents have no user channel — remove {banned!r}")


@pytest.mark.parametrize("name", INTERACTIVE_STEP_FILES)
def test_interactive_steps_exist_and_are_exempt(name: str) -> None:
    # Guard the exemption: if these files are renamed/removed, this test fails
    # loudly so the scope guard stays honest. (Skip if the file is absent in a
    # given workflow revision.)
    path = AGENTS_DIR / name
    if not path.exists():
        pytest.skip(f"{name} not present in this workflow revision")
    assert path.read_text()  # non-empty

from __future__ import annotations

from imp.agents.tools import get_registry

# The eleven built-in tools rewritten in spec §1.
ALL_TOOLS = [
    "read_file", "edit_file", "write_file", "grep", "glob", "bash",
    "bash_output", "bash_kill", "symbol_outline", "imp_events", "imp_show",
]

# explorer's toolset: its 6 declared tools plus bash_output/bash_kill, which
# the provider auto-grants alongside bash. This is the budget scope for AC 2a.
EXPLORER_TOOLSET = [
    "read_file", "glob", "grep", "bash",
    "bash_output", "bash_kill", "symbol_outline", "imp_events",
]

# One distinctive phrase from each tool's NEW pinned text — proves the rewrite
# landed per tool (AC 1). Not the tool's own returned value: these are excerpts
# of the spec's pinned contract, independent of the description literal.
NEW_SENTINELS = {
    "read_file": "the trailing `\\r` IS content",
    "edit_file": "size/UTF-8 limits",
    "write_file": "creating parent dirs as needed",
    "grep": "Regex search over file contents",
    "glob": "Uses pathlib semantics",
    "bash": "output over 50 KB spills to a file",
    "bash_output": "Read new output from a background process",
    "bash_kill": "advances the output cursors",
    "symbol_outline": "Compact structural outline",
    "imp_events": "an ordered record of what was said",
    "imp_show": "Read a quest's durable record by id",
}

# Clauses the rewrite removes from every description (paths/sandbox live centrally
# now — spec §4 — and in the tool-reference skill).
REMOVED_CLAUSES = [
    "relative to the project root",
    "relative to the worktree root",
    "bounded by the sandbox",
]

# Tools whose dropped detail moved to the skill must advertise it.
SKILL_ADVERTISERS = [
    "read_file", "edit_file", "grep", "glob", "bash",
    "bash_output", "symbol_outline",
]


def _desc(name: str) -> str:
    return get_registry()[name].description


def test_each_description_carries_its_new_pinned_phrase() -> None:
    for name, sentinel in NEW_SENTINELS.items():
        assert sentinel in _desc(name), f"{name} missing new pinned text {sentinel!r}"


def test_no_description_keeps_a_removed_paths_or_sandbox_clause() -> None:
    for name in ALL_TOOLS:
        desc = _desc(name)
        for clause in REMOVED_CLAUSES:
            assert clause not in desc, f"{name} still has removed clause {clause!r}"


def test_relocating_tools_advertise_the_skill() -> None:
    for name in SKILL_ADVERTISERS:
        assert "tool-reference" in _desc(name), f"{name} must point to tool-reference"


def test_explorer_toolset_description_word_budget() -> None:
    total = sum(len(_desc(n).split()) for n in EXPLORER_TOOLSET)
    # AC 2a: down from ~1,230 words to ~500 or fewer (a 60%+ cut). The pinned
    # text totals 443 words; 500 is the ceiling the reduction must stay under.
    assert total <= 500, f"explorer toolset description budget exceeded: {total} words"

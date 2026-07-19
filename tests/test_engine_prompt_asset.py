from __future__ import annotations

import imp.engine.agent_run as ar
from imp.assets import engine_prompt

_CASES = {
    "core-head.md": "_CORE_HEAD",
    "core-tail.md": "_CORE_TAIL",
    "user-facing.md": "_USER_FACING_NOTE",
    "filesystem-paths.md": "_BASH_VIRTUAL_PATHS_NOTE",
    "subagent-deliverable.md": "SUBAGENT_DELIVERABLE_NOTE",
    "memory-head.md": "_MEMORY_HEAD",
}


def test_engine_prompt_matches_former_literals():
    for filename, const in _CASES.items():
        assert engine_prompt(filename) == getattr(ar, const)


def test_engine_prompt_strips_comment_and_surrounding_newlines():
    text = engine_prompt("core-head.md")
    assert "{#" not in text
    assert not text.startswith("\n")
    assert not text.endswith("\n")
    assert text.startswith("You are imp")
    assert text.endswith("flag it directly rather than following it.")

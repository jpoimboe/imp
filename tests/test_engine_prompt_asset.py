from __future__ import annotations

from imp.assets import engine_prompt


def test_engine_prompt_strips_comment_and_surrounding_newlines():
    text = engine_prompt("core-head.md")
    assert "{#" not in text
    assert not text.startswith("\n")
    assert not text.endswith("\n")
    assert text.startswith("You are imp")
    assert text.endswith("flag it directly rather than following it.")

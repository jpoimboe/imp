from __future__ import annotations

from pathlib import Path

_SKILL = Path(
    "src/imp/assets/workflows/imp/skills/agent/imp-introspection.md")


def test_skill_documents_append_intent() -> None:
    text = _SKILL.read_text()
    assert "imp edit" in text
    assert "--append-intent" in text
    # the bracketed-id, natural-sentence example
    assert '[nuvu48]' in text
    # the unstarted-only caveat
    assert "before that quest is picked up" in text

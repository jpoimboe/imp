from __future__ import annotations

from pathlib import Path

_SKILL = Path("docs/guide/introspection.md")


def test_skill_documents_append_intent() -> None:
    text = _SKILL.read_text()
    assert "imp edit" in text
    assert "--append-intent" in text
    # the bracketed-id, natural-sentence example
    assert '[nuvu48]' in text
    # the unstarted-only caveat
    assert "before that quest is picked up" in text


def test_introspection_documents_events_search() -> None:
    text = _SKILL.read_text()
    assert "imp events search <query>" in text
    assert "the structured-event sibling of `imp search`" in text
    assert "title + intent, since drafts have no events" in text
    assert "newest-matched quests with per-quest matched-event lines" in text

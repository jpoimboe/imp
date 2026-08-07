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


def test_reading_another_quest_credits_the_daemon_not_the_sandbox_floor() -> None:
    text = _SKILL.read_text()
    assert "mounts the whole host read-only" not in text
    assert "sibling quest homes are masked inside the bash sandbox" in text
    assert "because the unsandboxed daemon" in text
    assert "not the sandbox floor" in text
    # The one on-disk read that does work across the mask, and its two env pins.
    assert "Other projects' state directories *are* readable read-only" in text
    assert "`cd <other repo> && IMP_SESSION_SOCKET= IMP_STATE_DIR= imp log <id>`" in text

from __future__ import annotations

from pathlib import Path

_SKILL = Path("docs/guide/introspection.md")


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_skill_documents_append_intent() -> None:
    text = _SKILL.read_text()
    assert "imp edit" in text
    assert "--append-intent" in text
    # the bracketed-id, natural-sentence example
    assert '[nuvu48]' in text
    # the unstarted-only caveat
    assert "before that quest is picked up" in text


def test_introspection_documents_bounded_events_search_workflow() -> None:
    text = _SKILL.read_text()
    flat = _flat(text)
    assert "imp events search <query>" in text
    assert "the structured-event sibling of `imp search`" in text
    assert "title + intent, since drafts have no events" in text
    assert "active → backlog → archive" in flat
    assert "first three matched quests and the first three matching events per quest" in flat
    assert "`--limit N` changes only the matched-quest bound" in flat
    assert "`--list` scans every matched quest unless capped with `--limit N`" in flat
    assert "`--all` lifts both search-result bounds" in flat
    assert "`imp events <id> <range> --json`" in text


def test_reading_another_quest_credits_the_daemon_not_the_sandbox_floor() -> None:
    text = _SKILL.read_text()
    assert "mounts the whole host read-only" not in text
    assert "sibling quest homes are masked inside the bash sandbox" in text
    assert "because the unsandboxed daemon" in text
    assert "not the sandbox floor" in text
    # The one on-disk read that does work across the mask, and its two env pins.
    assert "Other projects' state directories *are* readable read-only" in text
    assert "`cd <other repo> && IMP_SESSION_SOCKET= IMP_STATE_DIR= imp log <id>`" in text
    # The demotion: the on-disk cross-project read is a fallback, not the history path.
    flat = _flat(text)
    assert "a fallback for cross-project diagnosis only" in flat
    assert ("**This** project's history is unreachable that way (its sibling quest homes "
            "and archive are masked); search it with `imp search` / `imp events search`, "
            "which the daemon resolves.") in flat


def test_introspection_warns_that_call_counts_measure_reach_not_effort() -> None:
    flat = _flat(_SKILL.read_text())
    para = (
        "**Counting tool calls measures reach, not effort.** An agent that "
        "can shell out reads files two ways, so a fall in `read_file` calls "
        "can conceal an equal rise in bash `cat`/`sed`/`head`/`grep`. Count "
        "bash reads as reads, and prefer a measure of what was touched — "
        "distinct files opened, overlap with the previous attempt — over "
        "call counts."
    )
    assert para in flat
    raw = flat.index("- **Raw** — `imp events <id> --json`")
    assert raw < flat.index(para) < flat.index("## Reading another quest")

from __future__ import annotations

from pathlib import Path

ASSETS = Path("src/imp/assets")
PARTIAL = ASSETS / "partials" / "commit-style.md"


def test_commit_style_partial_states_the_canonical_rule() -> None:
    text = PARTIAL.read_text()
    flat = " ".join(text.split())
    assert text.startswith("## Commit messages\n")
    # The message-file mandate, not an example of one.
    assert '`git commit -F "$TMPDIR/msg"` — never `-m`' in flat
    assert "**Subject:** `component: imperative summary` — no trailing period" in flat
    assert "under 100 characters" in flat
    # The corollary that kills the feat:/chore: reflex.
    assert (
        "names the **part of the codebase that changed**, not the kind of change"
    ) in flat
    assert "`git log --oneline -30 -- <the paths you touched>`" in flat
    assert "**Body:** wrapped at 75 columns, three movements, no headings:" in flat
    assert "1. **Context** — present tense, describing the deficient status quo." in flat
    assert "2. **Consequence** — what actually goes wrong" in flat
    assert "3. **The fix** — imperative mood." in flat
    assert "A body is required." in flat
    assert "**Voice:** impersonal" in flat
    assert "Never reference a commit from this same quest" in flat
    assert "git commit -m" not in text

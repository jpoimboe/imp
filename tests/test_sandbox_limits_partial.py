"""The sandbox-limits partial states imp's write bounds ahead of the debugging
hatches, and the old partial name survives nowhere under src/ or tests/."""

from __future__ import annotations

from pathlib import Path

PARTIAL = Path("src/imp/assets/partials/sandbox-limits.md")
# Split so the stale-name scan below never matches its own source file.
_OLD_NAME = "sandbox-" + "debugging"


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_partial_states_the_write_bounds_ahead_of_the_debugging_section() -> None:
    flat = _flat(PARTIAL.read_text())
    assert flat.startswith("## What you cannot write")
    assert (
        "binds all of `.git/refs/heads`, `.git/refs/stash`, and the main "
        "`.git/index` read-only" in flat)
    assert "re-exposing only `refs/heads/imp/`" in flat
    # The doctrine sentence: base refs are the engine's and the user's alone.
    assert "Base branches move only by imp's engine or by the user — never by you." in flat
    assert "give the user the exact command to run in their own shell" in flat
    # git's misleading error is called out, so an agent reads a denial correctly.
    assert "blames a write error rather than a permission" in flat
    # The pre-existing debugging content survives the rename, below the new section.
    assert flat.index("## What you cannot write") < flat.index("## Debugging live processes")


def test_no_source_or_test_file_references_the_old_partial_name() -> None:
    assert not (PARTIAL.parent / f"{_OLD_NAME}.md").exists()
    hits = [
        str(p)
        for root in (Path("src"), Path("tests"))
        for p in root.rglob("*")
        if p.is_file() and p.suffix in {".md", ".py", ".yaml", ".yml", ".sh"}
        and _OLD_NAME in p.read_text(errors="ignore")
    ]
    assert hits == []

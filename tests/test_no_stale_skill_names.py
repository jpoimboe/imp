# tests/test_no_stale_skill_names.py
from __future__ import annotations

import re
from pathlib import Path

# The two skills were renamed to imp-tool-reference / imp-driving-terminal-uis.
# No shipped file may still name a bare old slug — a match NOT already prefixed by
# "imp-". Scoped to the shipped tree (src/ + docs/); the spec doc and tests are out
# of scope for this guard.
_OLD = re.compile(r"(?<!imp-)(?:tool-reference|driving-terminal-uis)")
_ROOTS = (Path("src"), Path("docs"))


def _shipped_text_files() -> list[Path]:
    files: list[Path] = []
    for root in _ROOTS:
        files.extend(root.rglob("*.md"))
        files.extend(root.rglob("*.py"))
    return files


def test_no_shipped_file_names_the_old_skill_slugs() -> None:
    offenders = [
        str(path)
        for path in _shipped_text_files()
        if _OLD.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"stale old skill names remain in: {offenders}"

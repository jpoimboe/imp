from __future__ import annotations

from pathlib import Path


def test_no_lingering_removed_tool_references() -> None:
    """No `imp_show`/`imp_events` identifier survives in source or docs (AC 11b).

    Scoped to src/imp and docs — captured TUI fixtures under tests/fixtures are
    frozen recordings of runs that really did call the old tools and are left
    verbatim on purpose.
    """
    root = Path(__file__).resolve().parent.parent
    exts = {".py", ".md", ".yaml", ".yml", ".sh"}
    offenders = []
    for base in ("src/imp", "docs"):
        for p in (root / base).rglob("*"):
            if p.is_file() and p.suffix in exts:
                text = p.read_text(encoding="utf-8", errors="replace")
                if "imp_show" in text or "imp_events" in text:
                    offenders.append(str(p.relative_to(root)))
    assert offenders == [], f"lingering removed-tool references: {offenders}"

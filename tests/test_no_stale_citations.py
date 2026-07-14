"""AC 6c: no live in-repo citation of a moved/removed doc path or removed skill
name survives the reorg. Scans the shipped surfaces the spec enumerates (code,
prompts, skills, docs, the memory memo, README) — not tests/, whose move-guards
legitimately name the old paths; and not .imp/, the quest's own artifacts."""

from __future__ import annotations

from pathlib import Path

STALE = (
    "docs/what-is-imp.md",
    "docs/architecture.md",
    "docs/imp-conventions.md",
    "docs/imp-invariants.md",
    "docs/imp-operator-notes.md",
    "docs/subagent-authoring.md",
    "docs/tdd-analysis-report.md",
    "docs/tdd-brittleness-verdict.md",
    'load_skill(name="imp-introspection")',
    'load_skill(name="imp-memory")',
)

_ROOTS = ("src", "docs", "imp", "README.md")


def _repo_files():
    for root in _ROOTS:
        p = Path(root)
        if p.is_file():
            yield p
        elif p.is_dir():
            yield from (f for f in p.rglob("*") if f.is_file())


def test_no_stale_doc_or_skill_citations_remain() -> None:
    offenders = []
    for f in _repo_files():
        try:
            text = f.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for needle in STALE:
            if needle in text:
                offenders.append(f"{f}: {needle}")
    assert not offenders, "stale citations remain:\n" + "\n".join(offenders)

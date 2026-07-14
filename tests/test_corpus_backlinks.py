"""AC 6d: every corpus doc carries a soft back-link to the imp-knowledge hub as
its runtime entry point, and README links to the features catalog."""

from __future__ import annotations

from pathlib import Path

# Only the two corpus areas — NOT docs/ideas/, which holds non-corpus scratch notes.
CORPUS_DIRS = (Path("docs/guide"), Path("docs/internals"))
SENTINEL = "Part of imp's self-knowledge corpus"


def test_every_corpus_doc_backlinks_to_the_hub() -> None:
    docs = sorted(p for d in CORPUS_DIRS for p in d.rglob("*.md"))
    assert docs, "no corpus docs found"
    missing = [
        str(p) for p in docs
        if SENTINEL not in p.read_text() or "imp-knowledge" not in p.read_text()
    ]
    assert not missing, f"corpus docs missing the hub back-link: {missing}"


def test_readme_links_to_features_catalog() -> None:
    assert "docs/guide/features.md" in Path("README.md").read_text()

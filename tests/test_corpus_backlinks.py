"""AC 6d: every corpus doc carries a soft back-link to the imp-knowledge hub as
its runtime entry point."""

from __future__ import annotations

from tests._corpus import tracked

# The corpus is exactly these two areas; docs/ holds nothing else.
CORPUS_DIRS = ("docs/guide", "docs/internals")
SENTINEL = "Part of imp's self-knowledge corpus"


def test_every_corpus_doc_backlinks_to_the_hub() -> None:
    docs = tracked(*CORPUS_DIRS)
    assert docs, "no corpus docs found"
    missing = [
        str(p) for p in docs
        if SENTINEL not in p.read_text() or "imp-knowledge" not in p.read_text()
    ]
    assert not missing, f"corpus docs missing the hub back-link: {missing}"

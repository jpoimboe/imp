"""docs/guide/features.md — the capabilities catalog."""

from __future__ import annotations

from pathlib import Path

DOC = Path("docs/guide/features.md")


def test_features_doc_exists_and_is_nonempty() -> None:
    assert DOC.is_file()
    assert DOC.read_text().strip()


def test_features_names_every_catalogued_capability() -> None:
    low = DOC.read_text().lower()
    # drive from phone / remote
    assert "phone" in low
    # pause and resume mid-call
    assert "pause and resume" in low
    # per-role, multi-vendor model selection
    assert "per-role" in low
    assert "multi-vendor" in low
    # drafts & zones
    assert "draft" in low
    assert "backlog" in low
    assert "archive" in low
    # project memory
    assert "project memory" in low
    # the review gates
    assert "review gate" in low
    # event-log auditability & resumability
    assert "event log" in low


def test_features_catalogues_the_embedding_api_and_links_to_its_guide() -> None:
    body = DOC.read_text()
    assert "imp.api" in body
    assert "](embedding.md)" in body, "the catalog entry must link to the guide"

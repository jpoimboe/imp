"""docs/guide/lifecycle.md — quest lifecycle depth pulled from the hub."""

from __future__ import annotations

from pathlib import Path

DOC = Path("docs/guide/lifecycle.md")


def test_lifecycle_doc_exists_and_is_nonempty() -> None:
    assert DOC.is_file()
    assert DOC.read_text().strip()


def test_lifecycle_names_the_full_status_set_with_derived_running() -> None:
    body = DOC.read_text()
    for status in ("new", "paused", "saved", "completed",
                   "merge_blocked", "abandoned"):
        assert status in body, status
    # running is derived, never stored
    assert "running" in body
    assert "derived" in body


def test_lifecycle_covers_zones_and_materialize() -> None:
    low = DOC.read_text().lower()
    for zone in ("active", "backlog", "archive"):
        assert zone in low, zone
    assert "materialize" in low
    assert "create_and_enter_worktree" in DOC.read_text()


def test_lifecycle_names_the_three_left_draft_dispositions() -> None:
    body = DOC.read_text()
    assert "discard_conversation" in body
    assert "save_as_draft" in body
    assert "archive_conversation" in body


def test_lifecycle_carries_the_training_data_correction() -> None:
    body = DOC.read_text()
    # the correction: `imp new` runs save_as_draft (draft stays active), it does
    # NOT archive the draft as resumable.
    assert "training data" in body.lower()
    assert "imp new" in body

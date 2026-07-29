"""docs/guide/lifecycle.md — quest lifecycle depth pulled from the hub."""

from __future__ import annotations

from pathlib import Path

DOC = Path("docs/guide/lifecycle.md")


def test_lifecycle_doc_exists_and_is_nonempty() -> None:
    assert DOC.is_file()
    assert DOC.read_text().strip()


def test_lifecycle_names_the_full_status_set_with_derived_running() -> None:
    body = DOC.read_text()
    for status in ("new", "paused", "merge_blocked", "merged",
                   "shelved", "abandoned", "error"):
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
    # pin the correction paragraph itself: the wrong claim it explicitly rebuts
    assert "archives a draft as resumable" in body


def test_lifecycle_documents_waiting_error_and_no_draft_word() -> None:
    body = DOC.read_text()
    assert "waiting" in body
    assert "error" in body
    # running and waiting are derived from the lock + the durable halt-condition
    assert "halt-condition" in body or "halt condition" in body
    # the false "shown as a `draft`" display-word phrasing is gone
    assert "shown as a `draft`" not in body
    assert "relabeled `draft`" not in body
    assert "shown as a **draft**" not in body


def test_lifecycle_places_the_quest_home_outside_the_checkout() -> None:
    flat = " ".join(DOC.read_text().split())
    assert ("per-quest home on disk at `~/.imp/projects/<slug>/quests/<id>/`, "
            "outside the checkout") in flat
    # the in-worktree path is named as the symlink it is
    assert "reachable as `.imp/quests/<id>/`, a symlink" in flat

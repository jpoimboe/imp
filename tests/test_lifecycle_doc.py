"""docs/guide/lifecycle.md — quest lifecycle depth pulled from the hub."""

from __future__ import annotations

import re
from pathlib import Path

from imp.cli.exit_codes import ExitCode

DOC = Path("docs/guide/lifecycle.md")


def test_lifecycle_doc_exists_and_is_nonempty() -> None:
    assert DOC.is_file()
    assert DOC.read_text().strip()


def test_lifecycle_names_the_full_status_set_with_derived_running() -> None:
    body = DOC.read_text()
    for status in ("new", "paused", "merge_blocked", "merged",
                   "archived", "abandoned", "error"):
        assert status in body, status
    assert "(stored `archived`)" in body
    assert "shelved" not in body
    # running is derived, never stored
    assert "running" in body
    assert "derived" in body


def test_lifecycle_covers_zones_and_materialize() -> None:
    body = DOC.read_text()
    assert "\n## Zones: active list, backlog, history\n" in body
    for zone in ("- **active list** —", "- **backlog** —", "- **history** —"):
        assert zone in body, zone
    assert "- **archive** —" not in body
    assert "materialize" in body.lower()
    assert "create_and_enter_worktree" in body


def test_lifecycle_names_the_three_left_draft_dispositions() -> None:
    body = DOC.read_text()
    assert "discard_conversation" in body
    assert "save_as_draft" in body
    assert "archive_conversation" in body


def test_lifecycle_left_draft_section_names_the_leave_commands() -> None:
    body = DOC.read_text()
    section = body.split("\n## What happens to a left draft\n", 1)[1].split("\n## ", 1)[0]
    for command in ("`/pause`", "`/archive`", "`/clear`"):
        assert command in section, command
    assert "/save" not in section
    assert "/shelve" not in section


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


def test_lifecycle_documents_worktree_setup_on_go_live() -> None:
    body = DOC.read_text()
    assert "re-runs it on every `create_and_enter_worktree` call" in body


def test_lifecycle_exit_code_table_matches_the_exit_code_constants() -> None:
    body = DOC.read_text()
    assert "\n## Exit codes\n" in body
    section = body.split("\n## Exit codes\n", 1)[1].split("\n## ", 1)[0]
    codes = [int(c) for c in re.findall(r"^\| (\d+) \|", section, flags=re.M)]
    assert codes == sorted(int(code) for code in ExitCode)
    assert "| 130 | You ended it and nothing failed" in section
    assert ("left, paused, archived, discarded, abandoned or cancelled. "
            "A left quest is resumable") in section

"""Tokened `imp resume` below the blob floor: a token matching the unreadable
archived candidate refuses and names it, while a quest with a live home still
resolves — the `test_archive_floor_fallbacks` contract, downward."""
from __future__ import annotations

from pathlib import Path

import anyio
import pytest

import imp.cli.main as m
from imp.engine import archive
from imp.engine.abandon import archive_conversation
from imp.engine.errors import ArchiveMismatch
from imp.engine.quest import Quest
from imp.paths import project_state_dir
from tests.conftest import _seed_control

LIVE = "lodo40"
SHELVED = "lodo77"


def _live_and_shelved(control: Path) -> None:
    _seed_control(control)
    Quest.create_draft(control, title="Live", quest_id=LIVE,
                       base_branch="main", origin="idea")
    shelved = Quest.create_draft(control, title="Shelved", quest_id=SHELVED,
                                 base_branch="main")
    archive_conversation(shelved)
    assert not (project_state_dir(control) / "quests" / SHELVED).exists()


def _lower_data_floor(control: Path) -> None:
    archive._write_data_version(control, archive._DATA_VERSION - 1)


def _resolve(control: Path, token: str) -> str | int:
    async def run() -> str | int:
        return await m._resolve_resume_target(control, token, non_interactive=True)
    return anyio.run(run)


def test_a_current_marker_offers_both_candidates(tmp_path: Path) -> None:
    """Control: above the floor the archived resumable candidate IS offered."""
    _live_and_shelved(tmp_path)

    rows, unreadable = m._resumable_matches(tmp_path, "lodo")

    assert {r[0] for r in rows} == {LIVE, SHELVED}
    assert unreadable is None


def test_below_floor_a_live_id_resolves_to_itself(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _live_and_shelved(tmp_path)
    _lower_data_floor(tmp_path)

    rows, unreadable = m._resumable_matches(tmp_path, LIVE)
    target = _resolve(tmp_path, LIVE)

    assert {r[0] for r in rows} == {LIVE}
    assert unreadable is None
    assert target == LIVE
    assert capsys.readouterr().err == ""


def test_below_floor_a_token_matching_both_refuses_and_names_the_archived_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _live_and_shelved(tmp_path)
    _lower_data_floor(tmp_path)

    rows, unreadable = m._resumable_matches(tmp_path, "lodo")
    target = _resolve(tmp_path, "lodo")

    reason = str(ArchiveMismatch(floor="data",
                                 stored=archive._DATA_VERSION - 1,
                                 known=archive._DATA_VERSION))
    assert {r[0] for r in rows} == {LIVE}
    assert unreadable == m._UnreadableMatches(ids=(SHELVED,), reason=reason)
    assert target == 2
    assert capsys.readouterr().err == (
        f"imp: 'lodo' matches an archived quest this imp cannot read: {SHELVED}\n"
        f"  {reason}\n"
        f"  hint: readable match {LIVE} — pass its full id to resume it.\n")

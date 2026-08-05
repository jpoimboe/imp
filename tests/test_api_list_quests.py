"""imp.api.list_quests: one listing spanning in-flight and completed quests."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from imp.api import QuestSummary, create_quest, list_quests
from tests.conftest import _archive_with_event, _seed_control

_EVENT = json.dumps(
    {"id": "e1", "type": "step_started", "timestamp": "2026-06-01T00:00:00+00:00",
     "payload": {}}) + "\n"


def test_an_empty_control_root_lists_nothing(tmp_path: Path) -> None:
    _seed_control(tmp_path)

    assert list_quests(tmp_path) == []


def test_spans_in_flight_and_completed_with_status_distinguishing_them(
    tmp_path: Path,
) -> None:
    _seed_control(tmp_path)
    live = create_quest(tmp_path, "still going", title="Live one")
    _archive_with_event(tmp_path, "gone12", "Finished one", _EVENT)

    by_id = {s.id: s for s in list_quests(tmp_path)}

    assert set(by_id) == {live.id, "gone12"}
    assert by_id[live.id].status == "new"          # in-flight, never started
    assert by_id["gone12"].status == "abandoned"   # terminal, read from the archive
    assert by_id["gone12"].step == "spec"          # carried from the archived state


def test_summaries_carry_the_title_and_are_frozen(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    handle = create_quest(tmp_path, "go", title="Interview a user")

    (summary,) = list_quests(tmp_path)

    assert isinstance(summary, QuestSummary)
    assert summary.id == handle.id
    assert summary.title == "Interview a user"
    # A never-started quest has no step: None, not the CLI's "-" display placeholder.
    assert summary.step is None
    with pytest.raises(FrozenInstanceError):
        summary.title = "renamed"   # type: ignore[misc]


def test_a_completed_quest_alone_is_still_listed(tmp_path: Path) -> None:
    # The in-flight-only helper in remote/daemon/discover.py drops this row.
    _seed_control(tmp_path)
    _archive_with_event(tmp_path, "gone34", "Finished one", _EVENT)

    assert [s.id for s in list_quests(tmp_path)] == ["gone34"]


def test_an_archived_quest_survives_unreadable_blobs(tmp_path: Path) -> None:
    # The index knows the quest exists; dropping it because a blob went missing
    # would hide a completed quest from the listing entirely.
    from imp.engine.archive import blobs_root

    _seed_control(tmp_path)
    _archive_with_event(tmp_path, "gone56", "Finished one", _EVENT)
    for blob in blobs_root(tmp_path).rglob("*"):
        if blob.is_file():
            blob.unlink()

    (summary,) = list_quests(tmp_path)

    assert (summary.id, summary.title) == ("gone56", "Finished one")
    assert summary.status == "abandoned"


def test_the_listing_names_the_new_public_symbols(tmp_path: Path) -> None:
    import imp.api as api

    for name in ("list_quests", "QuestSummary"):
        assert name in api.__all__, name
        assert hasattr(api, name), name


def test_a_session_socket_in_the_env_does_not_retarget_the_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `make_store` hands back a socket-backed proxy whenever IMP_SESSION_SOCKET is
    # set, ignoring its control_root argument entirely. A library call must stay
    # bound to the root it was given, or an embedding consumer running inside an
    # imp-spawned process silently lists a foreign daemon's quests.
    _seed_control(tmp_path)
    handle = create_quest(tmp_path, "go", title="Mine")
    monkeypatch.setenv("IMP_SESSION_SOCKET", str(tmp_path / "nonexistent.sock"))

    assert [s.id for s in list_quests(tmp_path)] == [handle.id]

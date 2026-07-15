from __future__ import annotations

import json
from pathlib import Path

from imp.cli.store import CompletedRow, HomeDigest, QuestCard, Readiness, SearchHit, SearchZones
from imp.cli.store_proxy import (
    _card_from_wire,
    _card_to_wire,
    _hit_from_wire,
    _hit_to_wire,
    _home_from_wire,
    _home_to_wire,
    _readiness_from_wire,
    _readiness_to_wire,
    _row_from_wire,
    _row_to_wire,
    _zones_from_wire,
    _zones_to_wire,
    run_query,
)
from imp.engine.quest import Quest
from tests.conftest import _seed_control


def _json_round_trip(value: object) -> object:
    return json.loads(json.dumps(value))


def test_home_digest_survives_json_round_trip_including_bytes() -> None:
    home = HomeDigest(
        quest_id="q1",
        quest_json=b'{"title": "Q"}',
        state_json=None,
        meta={"title": "Q"},
        last_iso="2026-06-01T00:00:00+00:00",
        has_user_message=True,
        prose="hello",
        intent_title="do the thing",
    )
    assert _home_from_wire(_json_round_trip(_home_to_wire(home))) == home


def test_quest_card_none_and_bytes_survive_round_trip() -> None:
    assert _card_from_wire(_json_round_trip(_card_to_wire(None))) is None
    card = QuestCard(
        quest_id="q1", status="active", step="plan", last="", title="Q",
        created="", step_label=None, phase_label=None, base_branch="main",
        merge_commit=None, depends_on=["dep"], intent="why", summary=b"done\n")
    assert _card_from_wire(_json_round_trip(_card_to_wire(card))) == card


def test_completed_row_survives_round_trip() -> None:
    row = CompletedRow(
        quest_id="q1", status="merged", step="done", last="2026-06-01",
        title="Q", created="2026-05-01")
    assert _row_from_wire(_json_round_trip(_row_to_wire(row))) == row


def test_readiness_survives_round_trip_including_blocked_on_tuple() -> None:
    r = Readiness(ready=False, blocked_on=("dep1", "dep2"))
    got = _readiness_from_wire(_json_round_trip(_readiness_to_wire(r)))
    assert got == r
    assert isinstance(got.blocked_on, tuple)


def test_search_hit_survives_round_trip() -> None:
    hit = SearchHit(
        quest_id="q1", title="Q", zone="active", content="darkmode",
        last_activity=None)
    assert _hit_from_wire(_json_round_trip(_hit_to_wire(hit))) == hit


def test_search_zones_survives_round_trip_including_frozenset() -> None:
    z = SearchZones(
        active=True, backlog=False, statuses=frozenset({"merged", "abandoned"}),
        merged_after="2026-01-01")
    got = _zones_from_wire(_json_round_trip(_zones_to_wire(z)))
    assert got == z
    assert isinstance(got.statuses, frozenset)


def _append_event(quest: Quest, event: dict) -> None:
    with quest.events_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


def test_run_query_get_events_matches_local_store(tmp_path: Path) -> None:
    from imp.cli.store import LocalStore

    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="Live", quest_id="liveq", base_branch="main")
    _append_event(ch, {
        "id": "e1", "timestamp": "2026-06-01T00:00:00+00:00",
        "type": "user_message",
        "payload": {"human": True, "content": [{"type": "text", "text": "hi"}]}})
    store = LocalStore(tmp_path)

    wire = run_query(store, "get_events", {"quest_id": "liveq"})
    assert wire == store.get_events("liveq")
    assert any(e.get("type") == "user_message" for e in wire)


def test_run_query_backlog_readiness_encodes_blocked_on(tmp_path: Path) -> None:
    from imp.cli.store import LocalStore

    _seed_control(tmp_path)
    Quest.create(tmp_path, title="Dep", quest_id="depq", base_branch="main")
    wire = run_query(LocalStore(tmp_path), "backlog_readiness", {"depends_on": ["depq"]})
    assert wire == {"ready": False, "blocked_on": ["depq"]}


def test_run_query_search_decodes_zones(tmp_path: Path) -> None:
    from imp.cli.store import LocalStore

    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="Live One", quest_id="liveone", base_branch="main")
    _append_event(ch, {
        "id": "e1", "timestamp": "2026-06-01T00:00:00+00:00",
        "type": "user_message",
        "payload": {"human": True,
                    "content": [{"type": "text", "text": "darkmode toggle"}]}})
    zones = {"active": True, "backlog": False, "statuses": [], "merged_after": None}
    wire = run_query(LocalStore(tmp_path), "search", {"query": "darkmode", "zones": zones})
    assert [h["quest_id"] for h in wire] == ["liveone"]
    assert wire[0]["zone"] == "active"


def test_run_query_rejects_unknown_op(tmp_path: Path) -> None:
    from imp.cli.store import LocalStore

    _seed_control(tmp_path)
    try:
        run_query(LocalStore(tmp_path), "nope", {})
    except ValueError as exc:
        assert "nope" in str(exc)
    else:
        raise AssertionError("expected ValueError")

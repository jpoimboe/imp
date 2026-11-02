from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.cli.store import (
    CompletedRow,
    HomeDigest,
    NeighbourQuota,
    QuestCard,
    Readiness,
    SearchHit,
    SearchZones,
)
from imp.cli.store_proxy import (
    _card_from_wire,
    _card_to_wire,
    _hit_from_wire,
    _hit_to_wire,
    _home_from_wire,
    _home_to_wire,
    _neighbour_from_wire,
    _neighbour_to_wire,
    _readiness_from_wire,
    _readiness_to_wire,
    _row_from_wire,
    _row_to_wire,
    _zones_from_wire,
    _zones_to_wire,
    run_query,
)
from imp.engine.quest import Quest
from tests.conftest import _archive_with_event, _seed_control


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
        lock_live=True,
    )
    assert _home_from_wire(_json_round_trip(_home_to_wire(home))) == home


def test_quest_card_none_and_bytes_survive_round_trip() -> None:
    assert _card_from_wire(_json_round_trip(_card_to_wire(None))) is None
    card = QuestCard(
        quest_id="q1", status="paused", pause_reason="detached", step="plan",
        last="", title="Q", created="", step_label=None, phase_label=None,
        workflow="memory", base_branch="main",
        merge_commit=None, parent={"id": "par1", "title": "Parent"},
        depends_on=[{"id": "dep", "title": "Dep", "merged": True}],
        intent="why", seed_opening="summarize foo.py", summary=b"done\n",
        vars={"retries": 5, "strict": True})
    got = _card_from_wire(_json_round_trip(_card_to_wire(card)))
    assert got == card
    assert got is not None and got.pause_reason == "detached"
    assert got.workflow == "memory"
    assert got.seed_opening == "summarize foo.py"
    card2 = QuestCard(
        quest_id="q1", status="active", pause_reason=None, step="plan",
        last="", title="Q", created="", step_label=None, phase_label=None,
        workflow="default", base_branch="main",
        merge_commit=None, parent=None,
        depends_on=[{"id": "dep", "title": "Dep", "merged": True}],
        intent="why", seed_opening=None, summary=b"done\n", vars={})
    assert _card_from_wire(_json_round_trip(_card_to_wire(card2))) == card2


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
        "payload": {"subtype": "reply", "content": [{"type": "text", "text": "hi"}]}})
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
        "payload": {"subtype": "reply",
                    "content": [{"type": "text", "text": "darkmode toggle"}]}})
    zones = {"active": True, "backlog": False, "statuses": [], "merged_after": None}
    wire = run_query(LocalStore(tmp_path), "search", {"query": "darkmode", "zones": zones})
    assert [h["quest_id"] for h in wire] == ["liveone"]
    assert wire[0]["zone"] == "active"


def test_run_query_search_event_candidates_round_trips_none_last(tmp_path: Path) -> None:
    from imp.cli.store import LocalStore

    # tool_call event with NO timestamp -> indexed last_event_ts is None.
    line = json.dumps({
        "id": "e0", "type": "tool_call",
        "payload": {"name": "bash", "input": {"cmd": "GREPTARGET"}}}) + "\n"
    _archive_with_event(tmp_path, "arch01", "Archived one", line)
    store = LocalStore(tmp_path)

    local = store.search_event_candidates("GREPTARGET")
    assert local == [("arch01", "Archived one", "abandoned", None)]

    wire = run_query(store, "search_event_candidates", {"query": "GREPTARGET"})
    assert wire == [["arch01", "Archived one", "abandoned", None]]
    assert _json_round_trip(wire) == wire
    rebuilt = [tuple(x) for x in _json_round_trip(wire)]
    assert rebuilt == local


def test_neighbour_quota_survives_round_trip_including_the_agent_triples() -> None:
    n = NeighbourQuota(
        project="imp", quest_id="fego87",
        readings=({"id": "qk", "type": "quota_reading"},),
        usage=({"id": "qr", "type": "query_result"},),
        anchors=({"id": "pre", "type": "query_result"},),
        agents={"step:a": ("claude", "claude-opus-5", None)})
    got = _neighbour_from_wire(_json_round_trip(_neighbour_to_wire(n)))
    assert got == n
    assert isinstance(got.readings, tuple)
    assert isinstance(got.agents["step:a"], tuple)


_QUOTA_START = "2026-06-01T03:30:00+00:00"
_QUOTA_END = "2026-06-01T05:38:00+00:00"


def _neighbour_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quest_id: str,
    events: list[dict],
) -> None:
    """A live quest home in another project, addressed by path only."""
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    qdir = tmp_path / "imphome" / "projects" / "other" / "quests" / quest_id
    qdir.mkdir(parents=True)
    (qdir / "quest.json").write_text(json.dumps({
        "id": quest_id, "title": quest_id, "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00"}))
    (qdir / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events))


def test_run_query_quota_neighbours_ships_the_slice_not_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.cli.store import LocalStore

    _neighbour_home(tmp_path, monkeypatch, "fego87", [
        {"id": "am", "agent_id": "step:a",
         "timestamp": "2026-06-01T04:00:00+00:00", "type": "agent_message",
         "payload": {"provider": "codex", "model": "gpt-6.1-sol",
                     "auth_mode": "chatgpt", "usage": {"input_tokens": 7},
                     "stop_reason": None,
                     "raw": {"content": [{"type": "thinking",
                                          "thinking": "SECRETPROSE"}]}}},
        {"id": "noise", "agent_id": "step:a",
         "timestamp": "2026-06-01T04:10:00+00:00", "type": "tool_call",
         "payload": {"name": "bash", "input": {"cmd": "SECRETPROSE"}}},
        {"id": "qr", "agent_id": "step:a",
         "timestamp": "2026-06-01T04:30:00+00:00", "type": "query_result",
         "payload": {"session_id": "s1", "duration_ms": 30000, "usage": {},
                     "model_usage": {}}},
    ])
    store = LocalStore(tmp_path)

    wire = run_query(store, "quota_neighbours", {
        "quest_id": "samo71", "start_iso": _QUOTA_START,
        "end_iso": _QUOTA_END})

    assert _json_round_trip(wire) == wire
    # neither the excluded tool_call's prose nor the shipped agent_message's
    # own thinking text crosses the seam
    assert "SECRETPROSE" not in json.dumps(wire)
    assert wire[0]["quest_id"] == "fego87"
    assert [u["id"] for u in wire[0]["usage"]] == ["am", "qr"]
    assert wire[0]["usage"][0]["payload"] == {
        "provider": "codex", "model": "gpt-6.1-sol", "auth_mode": "chatgpt",
        "usage": {"input_tokens": 7}}
    assert wire[0]["agents"] == {"step:a": ["codex", "gpt-6.1-sol", "chatgpt"]}
    assert [_neighbour_from_wire(d) for d in wire] == store.quota_neighbours(
        "samo71", _QUOTA_START, _QUOTA_END)


def test_run_query_rejects_unknown_op(tmp_path: Path) -> None:
    from imp.cli.store import LocalStore

    _seed_control(tmp_path)
    try:
        run_query(LocalStore(tmp_path), "nope", {})
    except ValueError as exc:
        assert "nope" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_run_query_quest_card_carries_seed_opening(tmp_path: Path) -> None:
    # An unstarted draft has no intent event; its seed_opening must cross the proxy.
    from imp.cli.store import LocalStore
    _seed_control(tmp_path)
    Quest.create_draft(tmp_path, title="", quest_id="drft01", base_branch="main",
                       seed_opening="summarize foo.py")
    wire = run_query(LocalStore(tmp_path), "quest_card", {"quest_id": "drft01"})
    card = _card_from_wire(_json_round_trip(wire))
    assert card is not None
    assert card.seed_opening == "summarize foo.py"
    assert card.intent is None

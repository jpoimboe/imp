from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from imp.cli.store import LocalStore, SearchZones, make_store
from imp.engine import archive, worktree
from imp.engine.quest import LAYOUT_VERSION, Quest
from imp.engine.state import State
from tests.conftest import _seed_control, held_lock


def _append_event(quest: Quest, event: dict) -> None:
    with quest.events_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


def _archive(control: Path, quest_id: str, status: str = "merged") -> None:
    ch = Quest.load(control, quest_id)
    st = State.load(ch.state_dir)
    st.status = status
    st.save(ch.state_dir)
    archive.archive_quest(
        control, quest_id,
        events_path=ch.events_path,
        quest_json_path=ch.quest_dir / "quest.json",
        state_json_path=ch.state_dir / "state.json")
    worktree.remove_worktree(control, quest_id)


def test_make_store_returns_localstore(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    assert isinstance(make_store(tmp_path), LocalStore)


def test_get_events_live_archived_and_unknown(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="Live", quest_id="liveq", base_branch="main")
    _append_event(ch, {
        "id": "e1", "timestamp": "2026-06-01T00:00:00+00:00",
        "type": "user_message",
        "payload": {"role": "user", "subtype": "reply",
                    "content": [{"type": "text", "text": "hi there"}]}})
    store = make_store(tmp_path)

    live = store.get_events("liveq")
    assert live is not None
    assert any(e.get("type") == "user_message" for e in live)

    Quest.create(tmp_path, title="Gone", quest_id="goneq", base_branch="main")
    _archive(tmp_path, "goneq")
    archived = store.get_events("goneq")
    assert archived is not None and len(archived) >= 1

    assert store.get_events("nosuch") is None


def test_get_artifact_live_and_missing(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="Art", quest_id="artq", base_branch="main")
    spec_dir = ch.quest_dir / "artifacts" / "spec"
    spec_dir.mkdir(parents=True)
    (spec_dir / "artq.md").write_text("# the spec\n")
    store = make_store(tmp_path)

    assert store.get_artifact("artq", "spec") == b"# the spec\n"
    assert store.get_artifact("artq", "summary") is None


def test_completed_rows_from_archive(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="Done Thing", quest_id="doneq", base_branch="main")
    _archive(tmp_path, "doneq")
    rows = make_store(tmp_path).completed_rows()
    match = [r for r in rows if r.quest_id == "doneq"]
    assert len(match) == 1
    assert match[0].status == "merged"
    assert match[0].title == "Done Thing"


def test_completed_rows_empty_archive(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    assert make_store(tmp_path).completed_rows() == []


def test_scan_homes_digest_fields(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="Scan Me", quest_id="scanq", base_branch="main")
    _append_event(ch, {
        "id": "e1", "timestamp": "2026-06-01T00:00:00+00:00",
        "type": "user_message",
        "payload": {"role": "user", "subtype": "intent",
                    "content": [{"type": "text", "text": "wire the widget"}]}})
    _append_event(ch, {
        "id": "e2", "timestamp": "2026-06-01T00:00:05+00:00",
        "type": "user_message",
        "payload": {"role": "user", "subtype": "reply",
                    "content": [{"type": "text", "text": "please wire the widget"}]}})

    digs = {d.quest_id: d for d in make_store(tmp_path).scan_homes()}
    d = digs["scanq"]
    assert d.meta["title"] == "Scan Me"
    assert d.has_user_message is True
    assert "please wire the widget" in d.prose
    assert d.intent_title == "wire the widget"


def test_backlog_readiness_blocked_then_ready(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="Dep", quest_id="depq", base_branch="main")
    store = make_store(tmp_path)

    r = store.backlog_readiness(["depq"])
    assert r.ready is False
    assert r.blocked_on == ("depq",)

    _archive(tmp_path, "depq")
    r2 = store.backlog_readiness(["depq"])
    assert r2.ready is True
    assert r2.blocked_on == ()


def test_quest_card_composes_row_intent_and_summary(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="Card Q", quest_id="cardq", base_branch="main")
    _append_event(ch, {
        "id": "e1", "timestamp": "2026-06-01T00:00:00+00:00",
        "type": "user_message",
        "payload": {"role": "user", "subtype": "intent",
                    "content": [{"type": "text", "text": "build the widget"}]}})
    summ = ch.quest_dir / "artifacts" / "summary"
    summ.mkdir(parents=True)
    (summ / "cardq.md").write_text("all done\n")

    card = make_store(tmp_path).quest_card("cardq")
    assert card is not None
    assert card.quest_id == "cardq"
    assert card.title == "Card Q"
    assert card.base_branch == "main"
    assert card.intent == "build the widget"
    assert card.summary == b"all done\n"
    assert make_store(tmp_path).quest_card("nosuch") is None


def test_search_active_zone(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="Live One", quest_id="liveone", base_branch="main")
    _append_event(ch, {
        "id": "e1", "timestamp": "2026-06-01T00:00:00+00:00",
        "type": "user_message",
        "payload": {"role": "user", "subtype": "reply",
                    "content": [{"type": "text", "text": "darkmode toggle"}]}})
    store = make_store(tmp_path)

    zones = SearchZones(active=True, backlog=False, statuses=frozenset(),
                        merged_after=None)
    hits = store.search("darkmode", zones)
    assert [h.quest_id for h in hits] == ["liveone"]
    assert hits[0].zone == "active"

    assert store.search("no-such-text", zones) == []


def test_scan_homes_migrates_v5_home(tmp_path: Path) -> None:
    from imp.engine.quest import LAYOUT_VERSION

    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="", quest_id="v5q", base_branch="main")

    # Downgrade to v5 so the migration fires on next scan_homes.
    quest_json_path = ch.quest_dir / "quest.json"
    meta = json.loads(quest_json_path.read_text())
    meta["layout_version"] = 5
    quest_json_path.write_text(json.dumps(meta, indent=2, sort_keys=True))

    # Write v5 (pre-triad) events.  step_context sets prompt_pending for
    # agent1, so the following human:false user_message is reclassified to
    # engine_message/agent_prompt on migration.
    agent_id = "agent1"
    v5_events = [
        {
            "id": "ev0", "agent_id": None,
            "timestamp": "2026-06-01T00:00:00+00:00",
            "type": "opening_intent", "payload": {"text": ""},
        },
        {
            "id": "ev1", "agent_id": agent_id,
            "timestamp": "2026-06-01T00:00:01+00:00",
            "type": "step_context",
            "payload": {"system_prompt": "You are a helpful assistant.", "step": "s"},
        },
        {
            "id": "ev2", "agent_id": agent_id,
            "timestamp": "2026-06-01T00:00:02+00:00",
            "type": "user_message",
            "payload": {
                "role": "user", "human": False,
                "content": [{"type": "text", "text": "Framework prompt here"}],
            },
        },
        {
            "id": "ev3", "agent_id": agent_id,
            "timestamp": "2026-06-01T00:00:03+00:00",
            "type": "llm_response",
            "payload": {"role": "assistant",
                        "content": [{"type": "text", "text": "agent response"}]},
        },
        {
            "id": "ev4", "agent_id": agent_id,
            "timestamp": "2026-06-01T00:00:04+00:00",
            "type": "user_message",
            "payload": {
                "role": "user", "human": True,
                "content": [{"type": "text", "text": "genuine reply here"}],
            },
        },
    ]
    ch.events_path.write_text(
        "".join(json.dumps(ev) + "\n" for ev in v5_events)
    )

    digs = {d.quest_id: d for d in make_store(tmp_path).scan_homes()}
    assert "v5q" in digs
    d = digs["v5q"]

    # The genuine human reply is the title, NOT the engine-injected prompt.
    assert d.intent_title == "genuine reply here"

    # quest.json must be stamped to the current layout version.
    updated_meta = json.loads(quest_json_path.read_bytes())
    assert updated_meta["layout_version"] == LAYOUT_VERSION

    # events.jsonl must be rewritten in the triad schema.
    migrated = [
        json.loads(line)
        for line in ch.events_path.read_text().strip().splitlines()
    ]
    # Framework prompt became engine_message/agent_prompt.
    agent_prompts = [
        e for e in migrated
        if e.get("type") == "engine_message"
        and e.get("payload", {}).get("subtype") == "agent_prompt"
    ]
    assert len(agent_prompts) == 1
    # No user_message payload carries the legacy 'human' key.
    assert not any(
        "human" in e.get("payload", {})
        for e in migrated if e.get("type") == "user_message"
    )
    # The genuine reply is now subtype=reply.
    reply_events = [
        e for e in migrated
        if e.get("type") == "user_message"
        and e.get("payload", {}).get("subtype") == "reply"
    ]
    assert reply_events[0]["payload"]["content"][0]["text"] == "genuine reply here"

    # Second scan is idempotent — events.jsonl bytes must not change.
    events_bytes_after_first_scan = ch.events_path.read_bytes()
    make_store(tmp_path).scan_homes()
    assert ch.events_path.read_bytes() == events_bytes_after_first_scan


def test_scan_homes_tolerates_future_layout_version(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="Future", quest_id="futq", base_branch="main")

    # Stamp a future (unknowable) layout_version — migration must fail gracefully.
    quest_json_path = tmp_path / ".imp" / "quests" / "futq" / "quest.json"
    meta = json.loads(quest_json_path.read_text())
    meta["layout_version"] = 99
    quest_json_path.write_text(json.dumps(meta, indent=2, sort_keys=True))

    # A second, valid home to confirm the list still renders.
    ch2 = Quest.create(tmp_path, title="Good", quest_id="goodq", base_branch="main")
    _append_event(ch2, {
        "id": "e1", "timestamp": "2026-06-01T00:00:00+00:00",
        "type": "user_message",
        "payload": {"role": "user", "subtype": "intent",
                    "content": [{"type": "text", "text": "some intent"}]},
    })

    # Must not raise; both homes survive.
    digs = {d.quest_id: d for d in make_store(tmp_path).scan_homes()}
    assert "futq" in digs
    assert "goodq" in digs
    assert digs["goodq"].intent_title == "some intent"


def test_scan_homes_reports_lock_live(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    live = Quest.create(tmp_path, title="Live", quest_id="liveq", base_branch="main")
    dead = Quest.create(tmp_path, title="Dead", quest_id="deadq", base_branch="main")
    (live.quest_dir / "lock").write_text(str(os.getpid()))          # a living holder pid
    p = subprocess.Popen(["true"])
    p.wait()
    (dead.quest_dir / "lock").write_text(str(p.pid))               # a dead holder pid
    homes = {h.quest_id: h for h in LocalStore(tmp_path).scan_homes()}
    assert homes["liveq"].lock_live is True
    assert homes["deadq"].lock_live is False


def _seed_v11_store(control: Path, cid: str) -> Quest:
    q = Quest.create(control, title="T", quest_id=cid, base_branch="main")
    (q.state_dir / "state.json").write_text(json.dumps({
        "id": cid, "step": "code", "step_attempt": 0, "status": "paused",
        "resume_pending": "code", "resume_mode": "continue",
        "vars": {}, "loop_counts": {}, "current_step": None, "retry_feedback": None,
    }, indent=2, sort_keys=True))
    meta_path = q.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 11
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    return q


def _digest_version(control: Path, cid: str) -> int:
    return int(json.loads((_quest_dir(control, cid) / "quest.json").read_text())["layout_version"])


def _quest_dir(control: Path, cid: str) -> Path:
    return control / ".imp" / "quests" / cid


def test_scan_homes_migrates_idle_stale_home(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _seed_v11_store(tmp_path, "idles")
    homes = make_store(tmp_path).scan_homes()
    digest = next(h for h in homes if h.quest_id == "idles")
    assert digest.meta["layout_version"] == LAYOUT_VERSION
    assert _digest_version(tmp_path, "idles") == LAYOUT_VERSION


def test_scan_homes_skips_migration_under_held_lock(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    q = _seed_v11_store(tmp_path, "lives")
    with held_lock(q.quest_dir / "lock"):
        homes = make_store(tmp_path).scan_homes()
        digest = next(h for h in homes if h.quest_id == "lives")
        assert digest.meta["layout_version"] == 11
        assert digest.lock_live is True
        assert _digest_version(tmp_path, "lives") == 11

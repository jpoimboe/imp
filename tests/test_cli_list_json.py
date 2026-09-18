from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import imp.cli.main as m
from imp.engine import archive
from imp.engine.quest import LAYOUT_VERSION, Quest
from imp.engine.state import State
from imp.paths import project_state_dir
from tests.conftest import _archive_with_event, _seed_control
from tests.test_cli_list_filters import _archive_status
from tests.test_inflight_sort import _seed_inflight

# A 92-character, two-line opening prompt: longer than the board's 70-column
# NAME_CAP and carrying a newline the board would collapse to a space.
_ARCHIVED_INTENT_TEXT = (
    "teach the poller a retry budget\n"
    "then wire it through the config so a restart keeps the tally")

_ARCHIVED_INTENT_EVENT = json.dumps(
    {"id": "e1", "parent_id": None, "agent_id": None,
     "timestamp": "2026-06-07T00:00:00+00:00", "type": "user_message",
     "payload": {"role": "user", "subtype": "intent",
                 "content": [{"type": "text", "text": _ARCHIVED_INTENT_TEXT}]}}) + "\n"


def _seed_live(control: Path, cid: str, *, title: str, status: str = "running",
               step: str | None = "write", prompt: str | None = None,
               created: str = "2026-06-02T00:00:00+00:00",
               last: str = "2026-06-05T00:00:00+00:00") -> None:
    """A live, materialized quest home; `prompt` adds an opening-intent event."""
    cdir = project_state_dir(control) / "quests" / cid
    (cdir / "state").mkdir(parents=True)
    (cdir / "quest.json").write_text(json.dumps({
        "id": cid, "title": title, "base_branch": "main",
        "created_at": created, "materialized": True,
        "layout_version": LAYOUT_VERSION}))
    events: list[dict[str, Any]] = [
        {"type": "step_started", "agent_id": None, "timestamp": created,
         "payload": {"step": "write", "phase": "p", "phase_label": "impl"}}]
    if prompt is not None:
        events.append({"type": "user_message", "agent_id": None, "timestamp": last,
                       "payload": {"role": "user", "subtype": "intent",
                                   "content": [{"type": "text", "text": prompt}]}})
    (cdir / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events))
    State(id=cid, status=status, step=step, step_attempt=0,
          step_label="coding" if step else None,
          phase_label="impl" if step else None,
          started_at=created).save(cdir / "state")


def _archive_timestampless(control: Path, cid: str) -> None:
    """Archive a merged quest whose created_at and last-event timestamp are empty."""
    events = control / f"{cid}.jsonl"
    events.write_text(json.dumps({
        "id": f"{cid}-e0", "parent_id": None, "agent_id": None,
        "type": "step_started", "payload": {"id": cid}}) + "\n")
    quest_json = control / f"{cid}-quest.json"
    quest_json.write_text(json.dumps({
        "id": cid, "title": "Nostamp", "base_branch": "main",
        "created_at": "", "depends_on": [], "materialized": True}))
    state_json = control / f"{cid}-state.json"
    state_json.write_text(json.dumps({
        "id": cid, "status": "merged", "step": None, "merge_commit": "c0ffee"}))
    archive.archive_quest(control, cid, events_path=events,
                          quest_json_path=quest_json, state_json_path=state_json)


def test_board_json_orders_the_three_zones_and_tags_each(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _seed_inflight(tmp_path, "live01", created="2026-06-01T00:00:00+00:00",
                   started="2026-06-01T00:00:00+00:00")
    Quest.create_draft(tmp_path, title="Add OAuth", quest_id="idea03",
                       base_branch="main", origin="idea",
                       seed_opening="Add OAuth\nwire it up")
    _archive_status(tmp_path, "mrgd05", "merged", last_ts="2026-06-10T00:00:00+00:00")
    data = m._board_json(tmp_path, dispositions=set(m._DEFAULT_LIST_DISPOSITIONS),
                         cutoff=None)
    assert [(e["id"], e["zone"]) for e in data] == [
        ("live01", "active"), ("idea03", "backlog"), ("mrgd05", "archive")]


def test_board_json_active_entry_carries_the_core_keys_and_step(
        tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _seed_inflight(tmp_path, "live01", created="2026-06-01T00:00:00+00:00",
                   started="2026-06-01T00:00:00+00:00")
    assert m._board_json(tmp_path, dispositions={"active"}, cutoff=None) == [{
        "id": "live01", "zone": "active", "title": "LIVE01", "intent": None,
        "status": "running", "created": "2026-06-01T00:00:00+00:00",
        "last_activity": "2026-06-01T00:00:00+00:00",
        "step": {"phase": "impl", "name": "coding"}}]


def test_board_json_titled_row_reports_no_intent(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _seed_live(tmp_path, "namd07", title="Named",
               prompt="a prompt nobody should see")
    assert m._board_json(tmp_path, dispositions={"active"}, cutoff=None) == [{
        "id": "namd07", "zone": "active", "title": "Named", "intent": None,
        "status": "running", "created": "2026-06-02T00:00:00+00:00",
        "last_activity": "2026-06-05T00:00:00+00:00",
        "step": {"phase": "impl", "name": "coding"}}]


def test_board_json_unnamed_stepless_entry_reports_intent_and_omits_step(
        tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _seed_live(tmp_path, "blkd02", title="  ", status="merge_blocked", step=None,
               prompt="make the poller retry, please")
    assert m._board_json(tmp_path, dispositions={"active"}, cutoff=None) == [{
        "id": "blkd02", "zone": "active", "title": None,
        "intent": "make the poller retry, please", "status": "merge_blocked",
        "created": "2026-06-02T00:00:00+00:00",
        "last_activity": "2026-06-05T00:00:00+00:00"}]


def test_board_json_backlog_entry_reports_provenance_and_readiness(
        tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Quest.create_draft(tmp_path, title="Add OAuth", quest_id="idea03",
                       base_branch="main", origin="idea",
                       seed_opening="Add OAuth\nwire it up")
    Quest.create_draft(tmp_path, title="", quest_id="blok04",
                       base_branch="main", origin="followup",
                       depends_on=["nope99"], seed_opening="depends on a ghost")
    by_id = {e["id"]: e
             for e in m._board_json(tmp_path, dispositions={"backlog"}, cutoff=None)}
    assert set(by_id["idea03"]) == {
        "id", "zone", "title", "intent", "status", "created", "last_activity",
        "provenance", "readiness"}
    assert by_id["idea03"]["zone"] == "backlog"
    assert by_id["idea03"]["status"] == "backlog"
    assert by_id["idea03"]["provenance"] == "idea"
    assert by_id["idea03"]["readiness"] == "ready"
    assert by_id["idea03"]["title"] == "Add OAuth"
    assert datetime.fromisoformat(by_id["idea03"]["created"]).tzinfo is not None
    assert by_id["blok04"] == {
        "id": "blok04", "zone": "backlog", "title": None, "intent": None,
        "status": "backlog", "created": by_id["blok04"]["created"],
        "last_activity": by_id["blok04"]["last_activity"],
        "provenance": "followup", "readiness": "blocked"}


def test_board_json_archive_entry_omits_step_and_nulls_empty_timestamps(
        tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "mrgd05", "merged", last_ts="2026-06-10T00:00:00+00:00")
    _archive_timestampless(tmp_path, "nots01")
    by_id = {e["id"]: e
             for e in m._board_json(tmp_path, dispositions={"merged"}, cutoff=None)}
    assert by_id["mrgd05"] == {
        "id": "mrgd05", "zone": "archive", "title": "Mrgd05", "intent": None,
        "status": "merged", "created": "2026-06-02T00:00:00+00:00",
        "last_activity": "2026-06-10T00:00:00+00:00"}
    assert by_id["nots01"]["created"] is None
    assert by_id["nots01"]["last_activity"] is None


def test_board_json_unnamed_archived_entry_carries_its_intent_and_omits_step(
        tmp_path: Path) -> None:
    # `_archive_with_event` seeds an `abandoned` quest whose step is `"spec"`, so a
    # whole-entry equality here pins both halves of the archive zone: its intent
    # comes from the gatherer's `_resolve_row_intent` backfill (a different path
    # from the active zone's scanned-home map), and it emits no `step` key.
    _seed_control(tmp_path)
    _archive_with_event(tmp_path, "unna08", "", _ARCHIVED_INTENT_EVENT)
    assert m._board_json(tmp_path, dispositions={"abandoned"}, cutoff=None) == [{
        "id": "unna08", "zone": "archive", "title": None,
        "intent": "teach the poller a retry budget\n"
                  "then wire it through the config so a restart keeps the tally",
        "status": "abandoned", "created": "2026-06-01T00:00:00+00:00",
        "last_activity": "2026-06-07T00:00:00+00:00"}]

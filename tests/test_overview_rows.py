from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import imp.cli.main as m
from imp.engine import archive
from imp.engine.quest import Quest
from tests.conftest import _seed_control
from tests.test_cli_list_filters import _archive_status
from tests.test_inflight_sort import _seed_inflight

_NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)


def _archive_unnamed_with_intent(control: Path, cid: str, *, prompt: str,
                                 last_ts: str = "2026-06-03T00:00:00+00:00") -> None:
    """Archive a title-less merged quest, with an opening prompt and NO live home."""
    events = control / f"{cid}.jsonl"
    events.write_text(
        json.dumps({"id": f"{cid}-e0", "parent_id": None, "agent_id": None,
                    "timestamp": last_ts, "type": "user_message",
                    "payload": {"role": "user", "subtype": "intent",
                                "content": [{"type": "text", "text": prompt}]}}) + "\n")
    quest_json = control / f"{cid}-quest.json"
    quest_json.write_text(json.dumps({
        "id": cid, "title": "", "base_branch": "main",
        "created_at": "2026-06-02T00:00:00+00:00", "depends_on": [],
        "materialized": True}))
    state_json = control / f"{cid}-state.json"
    state_json.write_text(json.dumps({
        "id": cid, "status": "merged", "step": None, "merge_commit": "c0ffee"}))
    archive.archive_quest(control, cid, events_path=events,
                          quest_json_path=quest_json, state_json_path=state_json)


def test_history_by_disposition_selects_only_the_wanted_dispositions(
        tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "mrgd", "merged", last_ts="2026-06-10T00:00:00+00:00")
    _archive_status(tmp_path, "abnd", "abandoned", last_ts="2026-06-09T00:00:00+00:00")
    _archive_status(tmp_path, "shlv", "shelved", last_ts="2026-06-08T00:00:00+00:00")
    rows = m._history_by_disposition(
        tmp_path, dispositions={"abandoned", "shelved"}, live_ids=set())
    assert [r[0] for r in rows] == ["abnd", "shlv"]   # newest-first; merged excluded
    # The live-zone tokens name no archive status, so they select nothing.
    assert m._history_by_disposition(
        tmp_path, dispositions={"active", "backlog"}, live_ids=set()) == []


def test_history_by_disposition_dedups_rows_with_a_live_home(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "mrgd", "merged", last_ts="2026-06-10T00:00:00+00:00")
    assert m._history_by_disposition(
        tmp_path, dispositions={"merged"}, live_ids={"mrgd"}) == []


def test_overview_rows_groups_and_orders_the_three_zones(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _seed_inflight(tmp_path, "late03", created="2026-06-01T00:00:00+00:00",
                   started="2026-06-03T00:00:00+00:00")
    _seed_inflight(tmp_path, "erly03", created="2026-06-02T00:00:00+00:00",
                   started="2026-06-01T00:00:00+00:00")
    Quest.create_draft(tmp_path, title="Brief", quest_id="bk1",
                       base_branch="main", origin="idea", seed_opening="Brief")
    _archive_status(tmp_path, "mrgd", "merged", last_ts="2026-06-10T00:00:00+00:00")
    _archive_status(tmp_path, "oldm", "merged", last_ts="2026-06-04T00:00:00+00:00")
    rows = m._overview_rows(
        tmp_path, dispositions={"active", "backlog", "merged", "done"}, cutoff=None)
    assert [r[0] for r in rows.inflight] == ["erly03", "late03"]  # earliest start first
    assert [r[0] for r in rows.backlog] == ["bk1"]
    assert [r[0] for r in rows.history] == ["mrgd", "oldm"]       # newest-first


def test_overview_rows_gates_each_zone_on_dispositions(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _seed_inflight(tmp_path, "live04", created="2026-06-01T00:00:00+00:00",
                   started="2026-06-01T00:00:00+00:00")
    Quest.create_draft(tmp_path, title="Brief", quest_id="bk2",
                       base_branch="main", origin="idea", seed_opening="Brief")
    _archive_status(tmp_path, "mrgd", "merged", last_ts="2026-06-10T00:00:00+00:00")

    merged_only = m._overview_rows(tmp_path, dispositions={"merged"}, cutoff=None)
    assert merged_only.inflight == [] and merged_only.backlog == []
    assert [r[0] for r in merged_only.history] == ["mrgd"]

    backlog_only = m._overview_rows(tmp_path, dispositions={"backlog"}, cutoff=None)
    assert [r[0] for r in backlog_only.backlog] == ["bk2"]
    assert backlog_only.inflight == [] and backlog_only.history == []

    active_only = m._overview_rows(tmp_path, dispositions={"active"}, cutoff=None)
    assert [r[0] for r in active_only.inflight] == ["live04"]
    assert active_only.backlog == [] and active_only.history == []


def test_overview_rows_cutoff_drops_rows_older_than_the_cutoff(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "fresh", "merged", last_ts="2026-07-14T00:00:00+00:00")
    _archive_status(tmp_path, "stale", "merged", last_ts="2026-06-01T00:00:00+00:00")
    rows = m._overview_rows(tmp_path, dispositions={"merged"},
                            cutoff=datetime(2026, 7, 13, tzinfo=UTC))
    assert [r[0] for r in rows.history] == ["fresh"]


def test_overview_rows_backfills_intent_for_a_titleless_archived_row(
        tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_unnamed_with_intent(tmp_path, "unn002",
                                 prompt="add a retry budget to the poller")
    rows = m._overview_rows(tmp_path, dispositions={"merged"}, cutoff=None)
    assert [r[0] for r in rows.history] == ["unn002"]
    assert rows.intents["unn002"] == "add a retry budget to the poller"


def test_overview_rows_history_selects_both_boards(
        tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "mrgd", "merged", last_ts="2026-06-10T00:00:00+00:00")
    _archive_status(tmp_path, "abnd", "abandoned", last_ts="2026-06-09T00:00:00+00:00")
    _archive_status(tmp_path, "shlv", "shelved", last_ts="2026-06-08T00:00:00+00:00")
    default = {"active", "backlog", "merged", "done"}
    plain = m._overview_rows(tmp_path, dispositions=default, cutoff=None)
    every = m._overview_rows(
        tmp_path, dispositions=default | {"abandoned", "shelved"}, cutoff=None)
    assert [r[0] for r in plain.history] == ["mrgd"]
    assert [r[0] for r in every.history] == ["mrgd", "abnd", "shlv"]


def test_overview_lines_routes_through_the_shared_gatherer(
        tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "mrgd", "merged", last_ts="2026-06-10T00:00:00+00:00")
    _archive_status(tmp_path, "abnd", "abandoned", last_ts="2026-06-09T00:00:00+00:00")
    asked: list[set[str]] = []
    real = m._overview_rows

    def spy(control: Path, *, dispositions: set[str],
            cutoff: datetime | None) -> m._OverviewRows:
        asked.append(set(dispositions))
        return real(control, dispositions=dispositions, cutoff=cutoff)

    monkeypatch.setattr(m, "_overview_rows", spy)

    all_lines = m._overview_lines(tmp_path, show_all=True, now=_NOW, color=False)
    assert asked == [{"active", "backlog", "merged", "done", "abandoned", "shelved"}]
    assert any(ln.startswith("abnd") for ln in all_lines)

    asked.clear()
    plain_lines = m._overview_lines(tmp_path, show_all=False, now=_NOW, color=False)
    assert asked == [{"active", "backlog", "merged", "done"}]
    assert not any(ln.startswith("abnd") for ln in plain_lines)
    assert any(ln.startswith("mrgd") for ln in plain_lines)

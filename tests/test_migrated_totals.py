"""AC 12 end-to-end: a log written before this quest renders corrected totals
after migration, on both carriers, and a re-run changes nothing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.cli.render_summary import build_summary, summary_totals
from imp.engine import archive
from imp.engine.archive import record
from imp.engine.archive.blobstore import LocalBlobStore
from imp.engine.quest import Quest
from tests.conftest import _seed_control

_A = "step:build/coder"

# Written before this quest: no session_id anywhere, and three cumulative
# query_result rows whose naive sum (3.00 + 12.50 + 6.40 = 21.90) is the
# over-report. The step_started between rows 2 and 3 ends the first series, so
# the true figure is 12.50 + 6.40 = 18.90.
_PRE_CHANGE = [
    {"id": "s0", "parent_id": None, "agent_id": None,
     "timestamp": "2026-06-02T03:00:00+00:00", "type": "step_started",
     "payload": {"phase": "build", "step": "coder",
                 "provider": "claude_code", "model": "claude-sonnet-4-6"}},
    {"id": "m1", "parent_id": None, "agent_id": _A,
     "timestamp": "2026-06-02T03:00:30+00:00", "type": "agent_message",
     "payload": {"provider": "claude_code", "model": "claude-sonnet-4-6",
                 "usage": {"input_tokens": 9999, "output_tokens": 8888},
                 "stop_reason": "end_turn"}},
    {"id": "q1", "parent_id": None, "agent_id": _A,
     "timestamp": "2026-06-02T03:01:00+00:00", "type": "query_result",
     "payload": {"total_cost_usd": 3.00, "usage": {},
                 "model_usage": {"claude-sonnet-4-6": {
                     "inputTokens": 100, "outputTokens": 10,
                     "cacheReadInputTokens": 0,
                     "cacheCreationInputTokens": 0}}}},
    {"id": "q2", "parent_id": None, "agent_id": _A,
     "timestamp": "2026-06-02T03:02:00+00:00", "type": "query_result",
     "payload": {"total_cost_usd": 12.50, "usage": {},
                 "model_usage": {"claude-sonnet-4-6": {
                     "inputTokens": 400, "outputTokens": 40,
                     "cacheReadInputTokens": 0,
                     "cacheCreationInputTokens": 0}}}},
    {"id": "s1", "parent_id": None, "agent_id": None,
     "timestamp": "2026-06-02T03:03:00+00:00", "type": "step_started",
     "payload": {"phase": "build", "step": "coder",
                 "provider": "claude_code", "model": "claude-sonnet-4-6"}},
    {"id": "m2", "parent_id": None, "agent_id": _A,
     "timestamp": "2026-06-02T03:03:30+00:00", "type": "agent_message",
     "payload": {"provider": "claude_code", "model": "claude-sonnet-4-6",
                 "usage": {"input_tokens": 7777, "output_tokens": 6666},
                 "stop_reason": "end_turn"}},
    {"id": "q3", "parent_id": None, "agent_id": _A,
     "timestamp": "2026-06-02T03:04:00+00:00", "type": "query_result",
     "payload": {"total_cost_usd": 6.40, "usage": {},
                 "model_usage": {"claude-sonnet-4-6": {
                     "inputTokens": 200, "outputTokens": 20,
                     "cacheReadInputTokens": 0,
                     "cacheCreationInputTokens": 0}}}},
    {"id": "s2", "parent_id": None, "agent_id": None,
     "timestamp": "2026-06-02T03:05:00+00:00", "type": "step_completed",
     "payload": {"step": "coder", "action": {"verb": "continue"}}},
]


def _seed_v16_home(control: Path, cid: str) -> Path:
    quest = Quest.create(control, title="", quest_id=cid, base_branch="main")
    quest.events_path.write_text(
        "".join(json.dumps(e) + "\n" for e in _PRE_CHANGE), encoding="utf-8")
    meta_path = quest.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 16
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    return quest.quest_dir


def _home_events(quest_dir: Path) -> list[dict]:
    text = (quest_dir / "events.jsonl").read_text()
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


def _seed_archived(control: Path, cid: str) -> None:
    store = LocalBlobStore(archive.blobs_root(control))
    events_hash = store.put(
        "".join(json.dumps(e) + "\n" for e in _PRE_CHANGE).encode("utf-8"))
    rec = record.build_record(
        {"id": cid, "title": "", "base_branch": "main",
         "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []},
        {"id": cid, "status": "completed", "step": "finalize"},
        events_hash)
    record.write_record(archive.records_root(control), rec)
    archive.rebuild_index(control)


def _blob_events(control: Path, cid: str) -> list[dict]:
    rec = record.read_record(archive.records_root(control), cid)
    assert rec is not None
    blob = LocalBlobStore(archive.blobs_root(control)).get(rec["events_hash"])
    assert blob is not None
    return [json.loads(ln) for ln in blob.decode().splitlines() if ln.strip()]


def _totals(events: list[dict]) -> tuple[int, float]:
    rows = build_summary(events).rows
    _work, total = summary_totals(rows, 300.0)
    return total.input_total + total.output, total.dollars


def test_an_unmigrated_log_mis_reports_the_cost() -> None:
    # The unmigrated log is wrong in its own way: with no session ids every
    # cumulative row of an agent folds into ONE bucket, so only the last
    # survives and the figure UNDER-reports (6.40 against the true 18.90).
    # Stated here so the corrected figure below is demonstrably produced BY the
    # migration rather than by the fixture happening to be right already.
    _tokens, cost = _totals([dict(e) for e in _PRE_CHANGE])
    assert cost == pytest.approx(6.40)


def test_a_migrated_live_home_renders_the_corrected_totals(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    quest_dir = _seed_v16_home(tmp_path, "mt001")

    Quest.load(tmp_path, "mt001")            # runs the migration chain

    tokens, cost = _totals(_home_events(quest_dir))
    assert cost == pytest.approx(18.90)      # 12.50 + 6.40, not the naive 21.90
    # Tokens come solely from model_usage: (400 + 40) + (200 + 20).
    assert tokens == 660


def test_a_migrated_archived_blob_renders_the_corrected_totals(
    tmp_path: Path,
) -> None:
    _seed_archived(tmp_path, "mt002")

    archive.ensure_current(tmp_path)

    tokens, cost = _totals(_blob_events(tmp_path, "mt002"))
    assert cost == pytest.approx(18.90)
    assert tokens == 660


def test_re_running_the_migration_leaves_the_totals_unchanged(
    tmp_path: Path,
) -> None:
    _seed_control(tmp_path)
    quest_dir = _seed_v16_home(tmp_path, "mt003")
    Quest.load(tmp_path, "mt003")
    first = _totals(_home_events(quest_dir))

    Quest.load(tmp_path, "mt003")            # already current: a no-op

    assert _totals(_home_events(quest_dir)) == first
    # session_id and round are the ONLY keys the chain injects; all else unchanged.
    for pre, post in zip(_PRE_CHANGE, _home_events(quest_dir), strict=True):
        extra = {"query_result": {"session_id"},
                 "step_started": {"round"}}.get(pre["type"], set())
        assert set(post["payload"]) == set(pre["payload"]) | extra

from __future__ import annotations

import json
import os
from pathlib import Path

from imp.engine.change import LAYOUT_VERSION
from imp.remote.daemon import discover
from imp.remote.drivers.telegram import event_body
from tests.conftest import _seed_control


def _seed_change(tmp_path: Path) -> str:
    """Create a minimal live Change worktree under tmp_path and return its id."""
    _seed_control(tmp_path)
    cid = "2026-06-14-aaaaaa"
    cdir = tmp_path / ".imp" / "quests" / cid
    (cdir / "state").mkdir(parents=True)
    (cdir / "state" / "state.json").write_text(
        json.dumps({"status": "new", "step": "implement"}))
    (cdir / "lock").write_text(str(os.getpid()))   # live holder ⇒ derived "running"
    (cdir / "quest.json").write_text(json.dumps({
        "id": cid, "title": "Dark mode", "base_branch": "main",
        "created_at": "2026-06-14T00:00:00Z",
        "layout_version": LAYOUT_VERSION}))
    (cdir / "events.jsonl").write_text("")
    return cid


def _append_event(tmp_path: Path, cid: str, event: dict) -> None:
    path = tmp_path / ".imp" / "quests" / cid / "events.jsonl"
    with path.open("a") as f:
        f.write(json.dumps(event) + "\n")


def _llm_response(text: str) -> dict:
    return {
        "type": "llm_response",
        "payload": {
            "provider": "anthropic",
            "raw": {"content": [{"type": "text", "text": text}]},
        },
    }


def test_snapshot_projects_status_step_title(tmp_path: Path) -> None:
    cid = _seed_change(tmp_path)
    assert discover.snapshot(tmp_path, cid) == ("running", "implement", "Dark mode")


def test_snapshot_is_none_for_unknown_change(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    assert discover.snapshot(tmp_path, "2026-06-14-nosuch") is None


def test_recent_narrative_returns_last_n_augmented_events_oldest_first(
    tmp_path: Path,
) -> None:
    cid = _seed_change(tmp_path)
    for word in ("first", "second", "third"):
        _append_event(tmp_path, cid, _llm_response(word))
    events = discover.recent_narrative(tmp_path, cid, 2)
    # The last two, oldest-first, as curated events — not bare strings.
    assert [event_body("llm_response", e["payload"]) for e in events] == [
        "second", "third"]
    assert all(e["type"] == "llm_response" for e in events)
    # Augmented with change context so header_line can render id · title.
    assert all(e["change"]["id"] == cid for e in events)
    assert all(e["change"]["title"] == "Dark mode" for e in events)


def test_recent_narrative_skips_tool_only_responses(tmp_path: Path) -> None:
    cid = _seed_change(tmp_path)
    _append_event(tmp_path, cid, _llm_response("hello"))
    _append_event(tmp_path, cid, {
        "type": "llm_response",
        "payload": {
            "provider": "anthropic",
            "raw": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]},
        },
    })
    events = discover.recent_narrative(tmp_path, cid, 5)
    assert [event_body("llm_response", e["payload"]) for e in events] == ["hello"]


def test_recent_narrative_is_empty_for_unknown_change(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    assert discover.recent_narrative(tmp_path, "2026-06-14-nosuch", 5) == []

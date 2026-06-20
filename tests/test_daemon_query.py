from __future__ import annotations

import json
from pathlib import Path

from imp.remote.daemon import discover
from tests.conftest import _seed_control


def _seed_change(tmp_path: Path) -> str:
    """Create a minimal live Change worktree under tmp_path and return its id."""
    _seed_control(tmp_path)
    cid = "2026-06-14-aaaaaa"
    cdir = tmp_path / ".imp" / "changes" / cid
    (cdir / "state").mkdir(parents=True)
    (cdir / "state" / "state.json").write_text(
        json.dumps({"status": "running", "step": "implement"}))
    (cdir / "change.json").write_text(json.dumps({
        "id": cid, "title": "Dark mode", "base_branch": "main",
        "created_at": "2026-06-14T00:00:00Z"}))
    (cdir / "events.jsonl").write_text("")
    return cid


def _append_event(tmp_path: Path, cid: str, event: dict) -> None:
    path = tmp_path / ".imp" / "changes" / cid / "events.jsonl"
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


def test_recent_narrative_returns_last_n_oldest_first(tmp_path: Path) -> None:
    cid = _seed_change(tmp_path)
    for word in ("first", "second", "third"):
        _append_event(tmp_path, cid, _llm_response(word))
    assert discover.recent_narrative(tmp_path, cid, 2) == ["second", "third"]


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
    assert discover.recent_narrative(tmp_path, cid, 5) == ["hello"]


def test_recent_narrative_is_empty_for_unknown_change(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    assert discover.recent_narrative(tmp_path, "2026-06-14-nosuch", 5) == []

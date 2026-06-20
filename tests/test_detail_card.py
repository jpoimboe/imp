from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from imp.cli import main
from imp.engine import backlog
from imp.engine.change import LAYOUT_VERSION
from imp.engine.state import State
from tests.conftest import _seed_control

_NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC)


def test_detail_card_backlog_source(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    backlog.write_record(
        tmp_path, change_id="2026-06-16-bkg111", title="Add OAuth",
        description="wire up OAuth login", depends_on=["2026-06-16-dep001"],
        created_by="human")
    card = "\n".join(main._detail_card(tmp_path, "2026-06-16-bkg111", now=_NOW))
    assert "2026-06-16-bkg111" in card
    assert "Add OAuth" in card
    assert "backlog" in card
    assert "blocked on 2026-06-16-dep001" in card   # readiness
    assert "depends_on:" in card                    # depends_on field present
    assert "wire up OAuth login" in card            # originating prompt


def _live_change(control: Path, change_id: str) -> None:
    cdir = control / ".imp" / "changes" / change_id
    cdir.mkdir(parents=True)
    (cdir / "state").mkdir()
    (cdir / "change.json").write_text(json.dumps({
        "id": change_id, "title": "Build widget", "base_branch": "main",
        "created_at": "2026-06-20T11:00:00+00:00",
        "depends_on": ["2026-06-16-dep001"],
        "layout_version": LAYOUT_VERSION}))
    (cdir / "events.jsonl").write_text(
        json.dumps({"type": "user_message", "agent_id": None,
                    "timestamp": "2026-06-20T11:00:00+00:00",
                    "payload": {"role": "user",
                                "content": [{"type": "text",
                                             "text": "build a widget please"}]}}) + "\n"
        + json.dumps({"type": "step_started", "agent_id": None,
                      "timestamp": "2026-06-20T11:00:01+00:00",
                      "payload": {"step": "spec-writer", "phase": "spec"}}) + "\n")
    State(id=change_id, status="paused", step="spec-writer",
          step_attempt=0).save(cdir / "state")


def test_detail_card_live_source(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _live_change(tmp_path, "2026-06-20-live01")
    card = "\n".join(main._detail_card(tmp_path, "2026-06-20-live01", now=_NOW))
    assert "2026-06-20-live01" in card
    assert "Build widget" in card
    assert "paused" in card
    assert "spec-writer" in card           # step
    assert "spec-writer (spec)" in card    # step + phase rendered together
    assert "main" in card                  # base_branch
    assert "2026-06-16-dep001" in card     # depends_on
    assert "build a widget please" in card  # originating prompt = first user_message

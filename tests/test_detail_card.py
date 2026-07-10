from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from imp.cli import main
from imp.engine.change import LAYOUT_VERSION, Change
from imp.engine.state import State
from tests.conftest import _seed_control

_NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC)


def test_detail_card_backlog_source(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create_draft(
        tmp_path,
        title="Add OAuth",
        quest_id="2026-06-16-bkg111",
        base_branch="main",
        origin="followup",
        depends_on=["2026-06-16-dep001"],
        seed_opening="Add OAuth\n\nwire up OAuth login",
    )
    card = "\n".join(main._detail_card(tmp_path, "2026-06-16-bkg111", now=_NOW))
    assert "2026-06-16-bkg111" in card
    assert "Add OAuth" in card
    assert "backlog" in card
    assert "followup" in card  # provenance shown
    assert "blocked on 2026-06-16-dep001" in card  # readiness from depends_on
    assert "depends_on:" in card
    assert "wire up OAuth login" in card  # seed_opening body
    created_line = next(ln for ln in card.splitlines() if ln.startswith("created:"))
    assert "+00:00" not in created_line               # tz offset trimmed on backlog cards too
    assert "." not in created_line.split("(")[0]      # no microseconds in the timestamp part


def _live_change(control: Path, quest_id: str) -> None:
    cdir = control / ".imp" / "quests" / quest_id
    cdir.mkdir(parents=True)
    (cdir / "state").mkdir()
    (cdir / "quest.json").write_text(
        json.dumps(
            {
                "id": quest_id,
                "title": "Build widget",
                "base_branch": "main",
                "created_at": "2026-06-20T11:00:00+00:00",
                "depends_on": ["2026-06-16-dep001"],
                "layout_version": LAYOUT_VERSION,
            }
        )
    )
    (cdir / "events.jsonl").write_text(
        json.dumps(
            {
                "type": "awaiting_user_input",
                "agent_id": None,
                "timestamp": "2026-06-20T10:59:59+00:00",
                "payload": {"prompt": "what should I build?"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "user_message",
                "agent_id": None,
                "timestamp": "2026-06-20T11:00:00+00:00",
                "payload": {
                    "role": "user",
                    "content": [{"type": "text", "text": "build a widget please"}],
                },
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "step_started",
                "agent_id": None,
                "timestamp": "2026-06-20T11:00:01+00:00",
                "payload": {
                    "step": "spec-writer",
                    "phase": "spec",
                    "phase_label": "spec phase",
                },
            }
        )
        + "\n"
    )
    State(id=quest_id, status="paused", step="spec-writer", step_label="writing spec",
          phase_label="spec phase", step_attempt=0).save(cdir / "state")


def _live_change_task_prompt(control: Path, quest_id: str) -> None:
    """A live change whose sole user_message is the composed task prompt (no
    opening_intent, no awaiting_user_input, no interject) -- the resumed-from-
    backlog shape whose card must show no prompt dump."""
    cdir = control / ".imp" / "quests" / quest_id
    cdir.mkdir(parents=True)
    (cdir / "state").mkdir()
    (cdir / "quest.json").write_text(
        json.dumps(
            {
                "id": quest_id,
                "title": "Resumed seed",
                "base_branch": "main",
                "created_at": "2026-06-20T11:00:00+00:00",
                "layout_version": LAYOUT_VERSION,
            }
        )
    )
    (cdir / "events.jsonl").write_text(
        json.dumps(
            {
                "type": "user_message",
                "agent_id": "step:implement/coder",
                "timestamp": "2026-06-20T11:00:00+00:00",
                "payload": {
                    "role": "user",
                    "content": [{"type": "text",
                                 "text": "## Task\nyou are a coder; do the thing"}],
                },
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "step_started",
                "agent_id": None,
                "timestamp": "2026-06-20T11:00:01+00:00",
                "payload": {"step": "coder", "phase": "implement"},
            }
        )
        + "\n"
    )
    State(id=quest_id, status="paused", step="coder", step_attempt=0).save(cdir / "state")


def test_detail_card_omits_synthetic_task_prompt_opening(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _live_change_task_prompt(tmp_path, "2026-06-20-task01")
    card = "\n".join(main._detail_card(tmp_path, "2026-06-20-task01", now=_NOW))
    assert "2026-06-20-task01" in card          # the change card still renders
    assert "Resumed seed" in card               # title shown
    assert "you are a coder" not in card        # no prompt-dump opening
    assert "## Task" not in card


def test_detail_card_live_source(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _live_change(tmp_path, "2026-06-20-live01")
    card = "\n".join(main._detail_card(tmp_path, "2026-06-20-live01", now=_NOW))
    assert "2026-06-20-live01" in card
    assert "Build widget" in card
    assert "paused" in card
    assert "spec phase · writing spec" in card  # <phase label> · <step label>
    assert "spec-writer" not in card  # never the raw step slug
    assert "(spec)" not in card  # never the old "<step> (<phase>)" raw-slug format
    assert "main" in card  # base_branch
    assert "2026-06-16-dep001" in card  # depends_on
    assert "build a widget please" in card  # originating prompt = first user_message


def test_detail_card_sources_phase_from_persisted_state(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    cdir = tmp_path / ".imp" / "quests" / "2026-06-20-src01"
    (cdir / "state").mkdir(parents=True)
    (cdir / "quest.json").write_text(json.dumps({
        "id": "2026-06-20-src01", "title": "Widget", "base_branch": "main",
        "created_at": "2026-06-20T11:00:00+00:00", "layout_version": LAYOUT_VERSION}))
    # The event log's phase_label ("EVENT PHASE") must NOT be the card's source.
    (cdir / "events.jsonl").write_text(json.dumps({
        "type": "step_started", "agent_id": None,
        "timestamp": "2026-06-20T11:00:01+00:00",
        "payload": {"step": "spec-writer", "phase": "spec",
                    "phase_label": "EVENT PHASE"}}) + "\n")
    State(id="2026-06-20-src01", status="paused", step="spec-writer",
          step_label="writing spec", phase_label="state phase",
          step_attempt=0).save(cdir / "state")
    card = "\n".join(main._detail_card(tmp_path, "2026-06-20-src01", now=_NOW))
    assert "state phase · writing spec" in card    # sourced from persisted field
    assert "EVENT PHASE" not in card               # not the event-log scan


def test_latest_phase_label_helper_removed() -> None:
    assert not hasattr(main, "_latest_phase_label")


def test_detail_card_created_trims_microseconds_and_offset(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    cdir = tmp_path / ".imp" / "quests" / "2026-06-20-tsx001"
    (cdir / "state").mkdir(parents=True)
    (cdir / "quest.json").write_text(json.dumps({
        "id": "2026-06-20-tsx001", "title": "Widget", "base_branch": "main",
        "created_at": "2026-06-20T11:00:00.123456+00:00",
        "layout_version": LAYOUT_VERSION}))
    (cdir / "events.jsonl").write_text("")
    State(id="2026-06-20-tsx001", status="paused", step="spec-writer",
          step_attempt=0).save(cdir / "state")
    card = "\n".join(main._detail_card(tmp_path, "2026-06-20-tsx001", now=_NOW))
    created_line = next(ln for ln in card.splitlines() if ln.startswith("created:"))
    assert "2026-06-20 11:00:00" in created_line   # trimmed to seconds, space-separated
    assert ".123456" not in created_line            # microseconds dropped
    assert "+00:00" not in created_line              # tz offset dropped
    assert "(" in created_line and ")" in created_line   # friendly age still present

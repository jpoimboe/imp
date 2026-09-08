"""The halt reason `imp show` surfaces: the store's token scan over the event log,
the card's `reason:` line, and the raw `pause_reason` JSON token."""

from __future__ import annotations

import json
import os
from pathlib import Path

from imp.cli.store import LocalStore
from imp.engine.quest import Quest
from imp.engine.state import State
from tests.conftest import _seed_control

_QID = "pau001"


def _paused(**payload: str) -> dict[str, object]:
    return {"type": "workflow_paused", "agent_id": None,
            "timestamp": "2026-06-20T11:05:00+00:00", "payload": payload}


def _event(etype: str) -> dict[str, object]:
    return {"type": etype, "agent_id": None,
            "timestamp": "2026-06-20T11:06:00+00:00", "payload": {}}


def _seed_quest(control: Path, *, status: str, events: list[dict[str, object]],
                lock_live: bool = False) -> Quest:
    """A live quest home whose persisted status and event log the test dictates.
    A lock naming this pid resolves the DISPLAY status to `running`."""
    ch = Quest.create(control, title="Widget", quest_id=_QID, base_branch="main")
    st = State.load(ch.state_dir)
    st.status = status
    st.step = "coder"
    st.save(ch.state_dir)
    with ch.events_path.open("a", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")
    if lock_live:
        (ch.quest_dir / "lock").write_text(str(os.getpid()))
    return ch


def test_store_card_reads_the_interrupt_reason_of_the_last_pause(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _seed_quest(tmp_path, status="paused",
                events=[_paused(reason="interrupt", interrupt_reason="detached")])
    card = LocalStore(tmp_path).quest_card(_QID)
    assert card is not None
    assert card.pause_reason == "detached"      # interrupt_reason beats reason


def test_store_card_reports_no_reason_for_a_your_turn_park(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _seed_quest(tmp_path, status="paused", events=[_paused(reason="awaiting_user")])
    card = LocalStore(tmp_path).quest_card(_QID)
    assert card is not None
    assert card.pause_reason is None


def test_store_card_drops_a_reason_a_later_resume_made_a_fossil(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _seed_quest(tmp_path, status="paused", events=[
        _paused(reason="interrupt", interrupt_reason="detached"),
        _event("workflow_resumed")])
    card = LocalStore(tmp_path).quest_card(_QID)
    assert card is not None
    assert card.pause_reason is None


def test_store_card_keeps_a_pause_that_followed_a_resume(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _seed_quest(tmp_path, status="paused", events=[
        _paused(reason="quota"),
        _event("workflow_resumed"),
        _paused(reason="interrupt", interrupt_reason="detached")])
    card = LocalStore(tmp_path).quest_card(_QID)
    assert card is not None
    assert card.pause_reason == "detached"      # the fossil guard re-arms per pause


def test_store_card_keeps_the_reason_of_a_saved_draft_conversation(
    tmp_path: Path,
) -> None:
    """conversation_saved ends a draft's segment, so the recorded token is current."""
    _seed_control(tmp_path)
    _seed_quest(tmp_path, status="paused", events=[
        _paused(reason="interrupt", interrupt_reason="ctrl-d"),
        _event("workflow_resumed"),
        _event("conversation_saved")])
    card = LocalStore(tmp_path).quest_card(_QID)
    assert card is not None
    assert card.pause_reason == "ctrl-d"

"""A stopped quest parked on an unanswered question reads `waiting` on every
surface: `imp list` (text and --json), the resume picker, and the `imp show` card."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import imp.cli.main as m
from imp.engine.quest import Quest
from imp.engine.state import HaltCondition, State
from tests.conftest import _seed_control

_QID = "qpk001"
_QUESTION = "Which DB should I use?"


def _question_parked(control: Path, *, halt: HaltCondition | None = "waiting",
                     interrupt: bool = False, lock_live: bool = False) -> Quest:
    """A threshold-step home stored `paused` whose events end in an unanswered gate."""
    _seed_control(control)
    ch = Quest.create_draft(control, title="Widget", quest_id=_QID,
                            base_branch="main", workflow="default")
    st = State.load(ch.state_dir)
    st.status = "paused"
    st.halt_condition = halt
    st.save(ch.state_dir)
    log = ch.event_log()
    log.append(agent_id=None, type="user_message", payload={
        "role": "user", "subtype": "intent",
        "content": [{"type": "text", "text": "wire up the widget"}]})
    log.append(agent_id=None, type="awaiting_user_input", payload={
        "steer_hint": "", "last_agent_message": _QUESTION, "interrupt": interrupt})
    if lock_live:
        (ch.quest_dir / "lock").write_text(str(os.getpid()))
    return ch


def _list_line(control: Path, monkeypatch: pytest.MonkeyPatch,
               capsys: pytest.CaptureFixture[str]) -> str:
    monkeypatch.setattr(m, "_control_root", lambda: control)
    monkeypatch.setenv("NO_COLOR", "1")
    assert m.main(["list"]) == 0
    return next(ln for ln in capsys.readouterr().out.splitlines() if _QID in ln)


def test_list_shows_a_question_parked_quest_as_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _question_parked(tmp_path)
    assert "waiting (threshold)" in _list_line(tmp_path, monkeypatch, capsys)


def test_list_shows_a_left_question_gate_as_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _question_parked(tmp_path, halt=None)
    line = _list_line(tmp_path, monkeypatch, capsys)
    assert "paused (threshold)" in line
    assert "waiting" not in line


def test_list_json_reports_a_question_parked_quest_as_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _question_parked(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    assert m.main(["list", "--json"]) == 0
    by_id = {e["id"]: e for e in json.loads(capsys.readouterr().out)}
    assert by_id[_QID]["status"] == "waiting"


def test_resume_picker_offers_a_question_parked_quest(tmp_path: Path) -> None:
    _question_parked(tmp_path)
    rows = {r[0]: r for r in m._resumable_rows(tmp_path)}
    assert rows[_QID][1] == "waiting"

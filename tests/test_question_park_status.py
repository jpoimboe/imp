"""A stopped quest parked on an unanswered question reads `waiting` on every
surface: `imp list` (text and --json), the resume picker, and the `imp show` card."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import imp.cli.main as m
from imp.cli.store import LocalStore
from imp.engine.quest import Quest
from imp.engine.state import HaltCondition, State
from tests.conftest import _seed_control
from tests.test_show_cli import _run

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


def _reason_line(value: str) -> str:
    return "reason:" + " " * 8 + value      # a 7-char label padded to the 15-column field


def _status_line(word: str) -> str:
    return "status:" + " " * 8 + word


def test_store_card_carries_the_pending_question(tmp_path: Path) -> None:
    _question_parked(tmp_path)
    card = LocalStore(tmp_path).quest_card(_QID)
    assert card is not None
    assert (card.status, card.question) == ("waiting", _QUESTION)


def test_show_names_the_question_right_after_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _question_parked(tmp_path)
    lines = _run(monkeypatch, tmp_path, "show", _QID).splitlines()
    i = lines.index(_status_line("waiting"))
    assert lines[i + 1] == _reason_line(
        f"a question needs your answer (imp resume {_QID})")


def test_show_puts_the_question_section_right_before_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _question_parked(tmp_path)
    lines = _run(monkeypatch, tmp_path, "show", _QID).splitlines()
    q, i = lines.index("question:"), lines.index("intent:")
    assert q < i
    assert lines[q - 1] == "" and lines[i - 1] == ""
    body = lines[q + 1:i - 1]
    assert body and all(ln.strip() for ln in body)
    assert any(_QUESTION in ln for ln in body)


def test_show_json_carries_the_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _question_parked(tmp_path)
    data = json.loads(_run(monkeypatch, tmp_path, "show", _QID, "--json"))
    assert data["status"] == "waiting"
    assert data["question"] == _QUESTION
    assert data["pause_reason"] is None


def test_left_question_gate_shows_paused_with_its_leave_reason_and_no_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _question_parked(tmp_path, halt=None)
    ch.event_log().append(agent_id=None, type="workflow_paused", payload={
        "reason": "interrupt", "interrupt_reason": "ctrl-d"})
    lines = _run(monkeypatch, tmp_path, "show", _QID).splitlines()
    i = lines.index(_status_line("paused"))
    assert lines[i + 1] == _reason_line("Ctrl-D")
    assert "question:" not in lines
    data = json.loads(_run(monkeypatch, tmp_path, "show", _QID, "--json"))
    assert data["question"] is None


def test_live_interrupt_gate_shows_waiting_without_a_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _question_parked(tmp_path, interrupt=True, lock_live=True)
    lines = _run(monkeypatch, tmp_path, "show", _QID).splitlines()
    i = lines.index(_status_line("waiting"))
    assert not lines[i + 1].startswith("reason:")
    assert "question:" not in lines


def test_show_renders_a_multi_paragraph_question_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _question_parked(tmp_path)
    ch.event_log().append(agent_id=None, type="awaiting_user_input", payload={
        "steer_hint": "", "interrupt": False,
        "last_agent_message": "Two options exist.\n\nWhich DB should I use?"})
    lines = _run(monkeypatch, tmp_path, "show", _QID).splitlines()
    q, i = lines.index("question:"), lines.index("intent:")
    body = "\n".join(lines[q + 1:i])
    assert "Two options exist." in body and "Which DB should I use?" in body

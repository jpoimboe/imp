"""The halt reason `imp show` surfaces: the store's token scan over the event log,
the card's `reason:` line, and the raw `pause_reason` JSON token."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from imp.cli.store import LocalStore
from imp.engine.quest import Quest
from imp.engine.state import State
from tests.conftest import _seed_control
from tests.test_show_cli import _run

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


def _reason_line(value: str) -> str:
    # `_card_field(label, value, 15)`: a 7-char label padded to the 15-column field
    return "reason:" + " " * 8 + value


def _status_line(word: str) -> str:
    return "status:" + " " * 8 + word


def test_show_card_names_the_detached_reason_right_after_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    _seed_quest(tmp_path, status="paused",
                events=[_paused(reason="interrupt", interrupt_reason="detached")])
    lines = _run(monkeypatch, tmp_path, "show", _QID).splitlines()
    i = lines.index(_status_line("paused"))
    assert lines[i + 1] == _reason_line("the terminal went away")


def test_show_json_carries_the_raw_pause_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    _seed_quest(tmp_path, status="paused",
                events=[_paused(reason="interrupt", interrupt_reason="detached")])
    data = json.loads(_run(monkeypatch, tmp_path, "show", _QID, "--json"))
    assert data["pause_reason"] == "detached"      # the token, not the card phrase


@pytest.mark.parametrize(("token", "phrase"), [
    ("ctrl-c", "Ctrl-C"),
    ("ctrl-d", "Ctrl-D"),
])
def test_show_card_names_a_keystroke_interrupt_in_display_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str, phrase: str,
) -> None:
    _seed_control(tmp_path)
    _seed_quest(tmp_path, status="paused",
                events=[_paused(reason="interrupt", interrupt_reason=token)])
    lines = _run(monkeypatch, tmp_path, "show", _QID).splitlines()
    i = lines.index(_status_line("paused"))
    assert lines[i + 1] == _reason_line(phrase)


_CARD_REASONS = [
    ("ctrl-d", "Ctrl-D"),
    ("ctrl-c", "Ctrl-C"),
    ("pause", "/pause"),
    ("sighup", "SIGHUP"),
    ("sigquit", "SIGQUIT"),
    ("sigterm", "interrupted"),
    ("detached", "the terminal went away"),
    ("max_loops", "a step hit its loop limit"),
    ("quota", "quota exhausted"),
    ("reauth_needed", "provider login expired"),
    ("sandbox_spawn_failed", "the sandbox could not start"),
    ("terminated", "agent error"),
    ("internal_error", "internal error"),
    ("api", "API error"),
    ("context_window", "a step used up its context handoffs"),
    ("config", "config error"),
    ("content_filter", "content filtered"),
    ("loop", "loop detected"),
    ("unknown", "unknown error"),
    ("retry_ceiling", "retries exhausted"),
    ("auth", "auth failed"),
    ("auth_terminal", "auth failed"),
    ("rate_limit", "rate limited"),
    ("network", "network error"),
    ("stalled", "stalled"),
    ("agent_output_missing", "no output"),
    ("script_blocked", "a script step was blocked"),
    ("clean_tree", "a step left uncommitted changes"),
    ("unknown_step", "the current step is missing from the workflow"),
    ("interrupt", "interrupted"),
    ("model-override", "model switch"),
    ("compact", "/compact"),
    ("awaiting_turn", "waiting for its turn to merge"),
]


@pytest.mark.parametrize(("token", "phrase"), _CARD_REASONS)
def test_show_card_phrases_every_halt_token_and_json_keeps_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str, phrase: str,
) -> None:
    _seed_control(tmp_path)
    _seed_quest(tmp_path, status="paused", events=[_paused(reason=token)])
    lines = _run(monkeypatch, tmp_path, "show", _QID).splitlines()
    i = lines.index(_status_line("paused"))
    assert lines[i + 1] == _reason_line(phrase)
    data = json.loads(_run(monkeypatch, tmp_path, "show", _QID, "--json"))
    assert data["pause_reason"] == token


@pytest.mark.parametrize(("reason", "phrase"), [
    ("quota", "quota exhausted"),
    ("sandbox_spawn_failed", "the sandbox could not start"),
    ("context_window", "a step used up its context handoffs"),
])
def test_show_names_an_error_halt_reason_under_status_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str, phrase: str,
) -> None:
    _seed_control(tmp_path)
    _seed_quest(tmp_path, status="error", events=[_paused(reason=reason)])
    lines = _run(monkeypatch, tmp_path, "show", _QID).splitlines()
    i = lines.index(_status_line("error"))
    assert lines[i + 1] == _reason_line(phrase)
    data = json.loads(_run(monkeypatch, tmp_path, "show", _QID, "--json"))
    assert data["pause_reason"] == reason


def test_show_prints_no_reason_line_while_the_quest_runs_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live lock resolves the display status to `running`: the recorded detached
    token describes a halt that is over."""
    _seed_control(tmp_path)
    _seed_quest(tmp_path, status="paused", lock_live=True,
                events=[_paused(reason="interrupt", interrupt_reason="detached")])
    lines = _run(monkeypatch, tmp_path, "show", _QID).splitlines()
    assert _status_line("running") in lines
    assert not [ln for ln in lines if ln.startswith("reason:")]
    data = json.loads(_run(monkeypatch, tmp_path, "show", _QID, "--json"))
    assert data["pause_reason"] is None


def test_show_prints_no_reason_line_for_merge_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """merge_blocked's token IS its status word, so a line would only restate it."""
    _seed_control(tmp_path)
    _seed_quest(tmp_path, status="merge_blocked",
                events=[_paused(reason="merge_blocked")])
    card = LocalStore(tmp_path).quest_card(_QID)
    assert card is not None and card.pause_reason == "merge_blocked"
    lines = _run(monkeypatch, tmp_path, "show", _QID).splitlines()
    assert _status_line("merge blocked") in lines
    assert not [ln for ln in lines if ln.startswith("reason:")]
    data = json.loads(_run(monkeypatch, tmp_path, "show", _QID, "--json"))
    assert data["pause_reason"] is None


@pytest.mark.parametrize(("status", "events"), [
    pytest.param("paused", [_paused(reason="awaiting_user")], id="your-turn-park"),
    pytest.param("paused", [_paused(reason="interrupt", interrupt_reason="detached"),
                            _event("workflow_resumed")], id="fossil-reason"),
])
def test_show_prints_no_reason_line_when_none_is_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str,
    events: list[dict[str, object]],
) -> None:
    _seed_control(tmp_path)
    _seed_quest(tmp_path, status=status, events=events)
    lines = _run(monkeypatch, tmp_path, "show", _QID).splitlines()
    assert _status_line("paused") in lines          # the card rendered
    assert not [ln for ln in lines if ln.startswith("reason:")]
    data = json.loads(_run(monkeypatch, tmp_path, "show", _QID, "--json"))
    assert data["pause_reason"] is None


def test_context_window_pause_still_halts_as_error() -> None:
    from imp.engine.state import classify_pause_halt
    assert classify_pause_halt("context_window", None) == "error"

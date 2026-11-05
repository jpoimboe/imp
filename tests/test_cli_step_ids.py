from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from imp.cli.main import main
from tests.test_cli_log_filters import _make_live_quest


def _step(step: str, label: str, phase: str, phase_label: str, mark: str) -> list[dict]:
    return [
        {"type": "step_started", "agent_id": None,
         "payload": {"step": step, "label": label,
                     "phase": phase, "phase_label": phase_label}},
        {"type": "warning", "agent_id": None,
         "payload": {"source": "s", "message": f"{mark} skipped", "detail": {}}},
        {"type": "step_completed", "agent_id": None, "payload": {"step": step}},
    ]


_THRESHOLD = _step("threshold", "chatting", "threshold", "scoping phase", "THRESHMARK")
_EVENTS = [
    *_THRESHOLD,
    *_step("spec-finalizer", "writing spec", "spec", "spec phase", "SPECMARK"),
    *_THRESHOLD,
    *_step("plan-writer", "writing plan", "plan", "planning phase", "PLANMARK"),
]
_STEPLESS = [{"type": "warning", "agent_id": None,
              "payload": {"source": "s", "message": "lone", "detail": {}}}]


def _run(monkeypatch: pytest.MonkeyPatch, control: Path, *argv: str) -> tuple[int, str, str]:
    import imp.cli.main as m
    monkeypatch.setattr(m, "_control_root", lambda: control)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = main(list(argv))
    return rc, out.getvalue(), err.getvalue()


@pytest.mark.parametrize("command", ["log", "events"])
def test_unknown_step_lists_distinct_ids_in_first_run_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str,
) -> None:
    _make_live_quest(tmp_path, "ids001", _EVENTS)
    rc, _out, err = _run(monkeypatch, tmp_path, command, "ids001", "--step", "ghost")
    assert rc == 2
    assert err == ("imp: no step 'ghost' in ids001; "
                   "steps: threshold, spec-finalizer, plan-writer\n")


@pytest.mark.parametrize("command", ["log", "events"])
def test_unknown_step_in_stepless_quest_lists_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str,
) -> None:
    _make_live_quest(tmp_path, "ids002", _STEPLESS)
    rc, _out, err = _run(monkeypatch, tmp_path, command, "ids002", "--step", "ghost")
    assert rc == 2
    assert err == "imp: no step 'ghost' in ids002\n"


@pytest.mark.parametrize("command", ["log", "events"])
def test_step_rejects_a_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str,
) -> None:
    _make_live_quest(tmp_path, "ids001", _EVENTS)
    rc, _out, err = _run(monkeypatch, tmp_path, command, "ids001", "--step", "chatting")
    assert rc == 2
    assert err == ("imp: no step 'chatting' in ids001; "
                   "steps: threshold, spec-finalizer, plan-writer\n")


def test_log_step_selects_raw_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_live_quest(tmp_path, "ids001", _EVENTS)
    rc, out, err = _run(monkeypatch, tmp_path, "log", "ids001", "--step", "threshold")
    assert (rc, err) == (0, "")
    assert "THRESHMARK" in out
    assert "SPECMARK" not in out


def test_summary_step_selects_raw_id_and_shows_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_live_quest(tmp_path, "ids001", _EVENTS)
    rc, out, err = _run(
        monkeypatch, tmp_path, "log", "ids001", "-s", "--step", "threshold")
    assert (rc, err) == (0, "")
    assert "chatting" in out
    assert "writing spec" not in out


def test_events_step_selects_raw_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_live_quest(tmp_path, "ids001", _EVENTS)
    rc, out, err = _run(monkeypatch, tmp_path, "events", "ids001", "--step", "threshold")
    assert (rc, err) == (0, "")
    assert "threshold" in out
    assert "spec-finalizer" not in out


def test_events_index_lists_raw_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_live_quest(tmp_path, "ids001", _EVENTS)
    rc, out, err = _run(monkeypatch, tmp_path, "events", "ids001")
    assert (rc, err) == (0, "")
    assert "threshold" in out
    assert "spec-finalizer" in out
    assert "chatting" not in out
    assert "writing spec" not in out

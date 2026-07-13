from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from imp.cli.main import main
from imp.engine.quest import LAYOUT_VERSION
from imp.engine.state import JsonlEventLog


def _make_live_quest(control: Path, cid: str, events: list[dict]) -> None:
    d = control / ".imp" / "quests" / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "quest.json").write_text(json.dumps({
        "id": cid, "title": cid.title(), "base_branch": "main",
        "project_root": str(control), "layout_version": LAYOUT_VERSION}))
    log = JsonlEventLog(d / "events.jsonl")
    for e in events:
        log.append(agent_id=e.get("agent_id"), type=e["type"],
                   payload=e.get("payload", {}))


def _run(monkeypatch: pytest.MonkeyPatch, control: Path, *argv: str) -> str:
    import imp.cli.main as m
    monkeypatch.setattr(m, "_control_root", lambda: control)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(list(argv))
    assert rc == 0, buf.getvalue()
    return buf.getvalue()


_EVENTS = [
    {"type": "step_started", "agent_id": None, "payload": {"step": "spec"}},
    {"type": "tool_call", "agent_id": "step:design/spec",
     "payload": {"id": "tc1", "name": "read_file", "input": {"path": "x.py"}}},
    {"type": "tool_result", "agent_id": "step:design/spec",
     "payload": {"id": "t1", "content": "ok", "error": None}},
    {"type": "step_completed", "agent_id": None, "payload": {"step": "spec"}},
]


def test_events_bare_prints_index(tmp_path, monkeypatch) -> None:
    _make_live_quest(tmp_path, "aaa111", _EVENTS)
    out = _run(monkeypatch, tmp_path, "events", "aaa111")
    assert "quest aaa111 · 4 events · 1 steps" in out
    assert "steps:" in out
    assert "#1-4" in out
    assert "types:" in out


def test_events_leaf_type_prints_digest_lines(tmp_path, monkeypatch) -> None:
    _make_live_quest(tmp_path, "aaa111", _EVENTS)
    out = _run(monkeypatch, tmp_path, "events", "aaa111", "--type", "tool_call")
    assert "#2" in out
    assert "tool_call" in out
    assert "[spec]" in out
    # index header absent — this is event-line mode.
    assert "steps:" not in out


def test_events_step_scoped_index(tmp_path, monkeypatch) -> None:
    _make_live_quest(tmp_path, "aaa111", _EVENTS)
    out = _run(monkeypatch, tmp_path, "events", "aaa111", "--step", "spec")
    assert "step spec · #1-4 · 4 events" in out
    assert "agents:" in out


def test_events_positional_range(tmp_path, monkeypatch) -> None:
    _make_live_quest(tmp_path, "aaa111", _EVENTS)
    out = _run(monkeypatch, tmp_path, "events", "aaa111", "2-3")
    ordinals = [ln.split()[0] for ln in out.splitlines() if ln.startswith("#")]
    assert ordinals == ["#2", "#3"]  # ordinals 1 and 4 not selected


def test_events_limit_alone_implies_all_last_n(tmp_path, monkeypatch) -> None:
    _make_live_quest(tmp_path, "aaa111", _EVENTS)
    out = _run(monkeypatch, tmp_path, "events", "aaa111", "--limit", "1")
    lines = [ln for ln in out.splitlines() if ln.startswith("#")]
    assert len(lines) == 1
    assert lines[0].startswith("#4")


def test_events_comma_and_repeat_type_equivalent(tmp_path, monkeypatch) -> None:
    _make_live_quest(tmp_path, "aaa111", _EVENTS)
    comma = _run(monkeypatch, tmp_path, "events", "aaa111",
                 "--type", "tool_call,tool_result")
    repeat = _run(monkeypatch, tmp_path, "events", "aaa111",
                  "--type", "tool_call", "--type", "tool_result")
    assert comma == repeat
    assert "#2" in comma and "#3" in comma


def test_events_default_id_is_most_recent(tmp_path, monkeypatch) -> None:
    _make_live_quest(tmp_path, "aaa111", _EVENTS)
    out = _run(monkeypatch, tmp_path, "events")  # no id -> most recent
    assert "quest aaa111" in out


def test_events_unknown_quest_errors(tmp_path, monkeypatch) -> None:
    import imp.cli.main as m
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["events", "nosuch"])
    assert rc == 2


def test_events_unknown_step_errors(tmp_path, monkeypatch) -> None:
    # An unknown --step errors in leaf mode too (parity with the container path).
    import imp.cli.main as m
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    _make_live_quest(tmp_path, "aaa111", _EVENTS)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["events", "aaa111", "--step", "nope", "--all"])
    assert rc == 2

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from imp.cli.main import main
from imp.engine.quest import LAYOUT_VERSION
from imp.engine.state import JsonlEventLog
from imp.paths import project_state_dir


def _make_live_quest(control: Path, cid: str, events: list[dict]) -> None:
    d = project_state_dir(control) / "quests" / cid
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
    monkeypatch.delenv("IMP_SESSION_SOCKET", raising=False)
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


def test_events_bare_no_id_prints_help_exit_2(tmp_path, monkeypatch, capsys) -> None:
    import imp.cli.main as m
    _make_live_quest(tmp_path, "aaa111", _EVENTS)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    rc = main(["events"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "usage: imp events" in captured.err
    assert "aaa111" not in captured.out


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


def test_events_json_index_parses(tmp_path, monkeypatch) -> None:
    _make_live_quest(tmp_path, "aaa111", _EVENTS)
    out = _run(monkeypatch, tmp_path, "events", "aaa111", "--json")
    obj = json.loads(out)
    assert obj["quest_id"] == "aaa111"
    assert obj["step_count"] == 1


def test_events_json_lines_include_timestamp(tmp_path, monkeypatch) -> None:
    _make_live_quest(tmp_path, "aaa111", _EVENTS)
    out = _run(monkeypatch, tmp_path, "events", "aaa111",
               "--type", "tool_call", "--json")
    obj = json.loads(out)
    assert obj[0]["type"] == "tool_call"
    assert "timestamp" in obj[0]
    assert obj[0]["ordinal"] == 2


def test_events_json_step_scoped_parses(tmp_path, monkeypatch) -> None:
    _make_live_quest(tmp_path, "aaa111", _EVENTS)
    out = _run(monkeypatch, tmp_path, "events", "aaa111", "--step", "spec", "--json")
    obj = json.loads(out)
    assert obj["step"] == "spec"
    assert obj["windows"] == [[1, 4]]
    assert isinstance(obj["agents"], list)


def test_events_malformed_range_errors(tmp_path, monkeypatch) -> None:
    import imp.cli.main as m
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    _make_live_quest(tmp_path, "aaa111", _EVENTS)
    for bad in ("5-x", "xyz"):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = main(["events", "aaa111", bad])
        assert rc == 2
        assert "range" in err.getvalue()


_WIRE_EVENTS = [
    {"type": "user_message", "agent_id": None,
     "payload": {"role": "user", "subtype": "intent",
                 "content": [{"type": "text", "text": "please do X"}]}},
    {"type": "engine_message", "agent_id": "step:build/plan",
     "payload": {"role": "user", "subtype": "system_prompt",
                 "content": [{"type": "text", "text": "You are a coder."}]}},
    {"type": "tool_call", "agent_id": "step:build/plan",
     "payload": {"id": "tc1", "name": "read_file", "input": {"path": "x.py"}}},
]


def test_events_wire_selects_only_user_and_engine(tmp_path, monkeypatch):
    _make_live_quest(tmp_path, "wire01", _WIRE_EVENTS)
    out = _run(monkeypatch, tmp_path, "events", "wire01", "--wire")
    assert "user_message" in out
    assert "engine_message" in out
    assert "tool_call" not in out


def test_events_subtype_reaches_system_prompt(tmp_path, monkeypatch):
    _make_live_quest(tmp_path, "wire01", _WIRE_EVENTS)
    out = _run(monkeypatch, tmp_path, "events", "wire01",
               "--subtype", "system_prompt")
    assert "You are a coder." in out
    assert "please do X" not in out


def test_events_full_widens_past_the_width_clip(tmp_path, monkeypatch) -> None:
    # Content is short (~38 chars) so the per-field cap still present until Task 3
    # neither truncates it nor injects a `…`; the assembled compact line still far
    # exceeds the forced 40-column width, so only the line-level clip bites.
    long_result = {
        "type": "tool_result", "agent_id": "step:d/spec",
        "payload": {"id": "t1", "error": False,
                    "content": "Y" * 30 + "TAILMARK"},
    }
    _make_live_quest(tmp_path, "bbb222", [long_result])
    monkeypatch.setenv("COLUMNS", "40")             # force a narrow clip width
    clipped = _run(monkeypatch, tmp_path, "events", "bbb222", "--all")
    full = _run(monkeypatch, tmp_path, "events", "bbb222", "--all", "--full")
    assert "…" in clipped                           # clipped to $COLUMNS
    assert "TAILMARK" not in clipped                # the tail was clipped off
    assert "TAILMARK" in full                       # --full lifts the clip
    assert "…" not in full


def test_events_compact_renders_context_occupancy_per_provider_shape(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events = [
        {"type": "agent_message", "agent_id": "step:build/coder",
         "payload": {"provider": "anthropic", "model": "claude-opus-5",
                     "stop_reason": "end_turn",
                     "usage": {"input_tokens": 10, "output_tokens": 2},
                     "context_tokens": 159_842,
                     "raw": {"content": [{"type": "text", "text": "one"}]}}},
        {"type": "agent_message", "agent_id": "step:build/coder",
         "payload": {"provider": "claude_code", "model": "claude-opus-5",
                     "stop_reason": "end_turn",
                     "usage": {"input_tokens": 10, "output_tokens": 2},
                     "context_tokens": 137_000, "context_window": 1_000_000,
                     "context_pct": 79.85263,
                     "raw": {"content": [{"type": "text", "text": "two"}]}}},
    ]
    _make_live_quest(tmp_path, "ccc333", events)
    out = _run(monkeypatch, tmp_path, "events", "ccc333", "--all", "--full")
    assert "ctx=159842/200000 (79.9%)" in out      # catalog window, computed pct
    assert "ctx=137000/1000000 (79.9%)" in out     # provider-reported window+pct

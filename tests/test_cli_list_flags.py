"""List-valued flags flatten at parse time; imp events non-TTY output is clean."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from imp.cli.main import _build_parser, main
from imp.engine.quest import LAYOUT_VERSION
from imp.engine.state import JsonlEventLog
from imp.paths import project_state_dir


def test_events_type_flattens_at_parse() -> None:
    args = _build_parser().parse_args(
        ["events", "q", "--type=a,b", "--type", "c"])
    assert args.type == ["a", "b", "c"]


def test_events_step_agent_range_flatten_at_parse() -> None:
    args = _build_parser().parse_args(
        ["events", "q", "--step=s1,s2", "--agent=x", "--range=1-2,5"])
    assert args.step == ["s1", "s2"]
    assert args.agent == ["x"]
    assert args.range == ["1-2", "5"]


def test_log_show_hide_flatten_at_parse() -> None:
    args = _build_parser().parse_args(
        ["log", "q", "--show=thinking,tools", "--hide", "tiers"])
    assert args.show == ["thinking", "tools"]
    assert args.hide == ["tiers"]


def test_absent_list_flags_default_none() -> None:
    args = _build_parser().parse_args(["events", "q"])
    assert args.type is None
    assert args.step is None
    assert args.agent is None
    assert args.range is None
    log_args = _build_parser().parse_args(["log", "q"])
    assert log_args.show is None
    assert log_args.hide is None


_EVENTS = [
    {"type": "step_started", "agent_id": None, "payload": {"step": "spec"}},
    {"type": "tool_call", "agent_id": "step:design/spec",
     "payload": {"id": "tc1", "name": "read_file", "input": {"path": "x.py"}}},
    {"type": "step_completed", "agent_id": None, "payload": {"step": "spec"}},
]


def _make_live_quest(control: Path, cid: str) -> None:
    d = project_state_dir(control) / "quests" / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "quest.json").write_text(json.dumps({
        "id": cid, "title": cid.title(), "base_branch": "main",
        "project_root": str(control), "layout_version": LAYOUT_VERSION}))
    log = JsonlEventLog(d / "events.jsonl")
    for e in _EVENTS:
        log.append(agent_id=e.get("agent_id"), type=e["type"],
                   payload=e.get("payload", {}))


def _run(monkeypatch: pytest.MonkeyPatch, control: Path, *argv: str) -> str:
    import imp.cli.main as m
    monkeypatch.setattr(m, "_control_root", lambda: control)
    buf = io.StringIO()  # not a tty -> _page_or_print bypasses the pager
    with redirect_stdout(buf):
        rc = main(list(argv))
    assert rc == 0, buf.getvalue()
    return buf.getvalue()


def test_events_non_tty_output_carries_no_ansi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_live_quest(tmp_path, "aaa111")
    index = _run(monkeypatch, tmp_path, "events", "aaa111")
    scoped = _run(monkeypatch, tmp_path, "events", "aaa111", "--step", "spec")
    lines = _run(monkeypatch, tmp_path, "events", "aaa111", "--type", "tool_call")
    for out in (index, scoped, lines):
        assert "\x1b[" not in out

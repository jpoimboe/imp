# tests/test_tui_app.py
from __future__ import annotations

from imp.cli import narrative as nv
from imp.cli.tui import NarrativeApp


def _ev(eid, etype, agent_id=None, **payload):
    return {"id": eid, "agent_id": agent_id, "type": etype, "payload": payload}


SAMPLE = [
    _ev("1", "change_created", change_id=1, title="demo"),
    _ev("2", "phase_started", phase="design"),
    _ev("3", "step_started", step="spec", model="m", provider="anthropic"),
    _ev("4", "thinking", agent_id="step:design/spec", content="abc"),
    _ev("5", "step_completed", step="spec", action="continue"),
]


def test_app_builds_lines_and_get_formatted_text_includes_header() -> None:
    app = NarrativeApp(nv.build_tree(SAMPLE), live=False, mouse=False)
    frags = app._get_formatted_text()
    text = "".join(t for _s, t, *rest in frags)
    assert "demo" in text and "design" in text


def test_app_cursor_starts_at_tail_in_log_mode() -> None:
    app = NarrativeApp(nv.build_tree(SAMPLE), live=False, mouse=False)
    assert app.cursor_line == len(app._lines) - 1

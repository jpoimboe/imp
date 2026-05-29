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


def _app(full=False):
    big = SAMPLE + [
        _ev("6", "phase_started", phase="impl"),
        _ev("7", "step_started", step="build", model="m", provider="anthropic"),
        _ev("8", "tool_call", agent_id="step:impl/build", id="b1", name="bash", input={}),
        _ev("9", "tool_result", agent_id="step:impl/build", id="b1", content="ok", error=False),
        _ev("10", "step_completed", step="build", action="continue"),
    ]
    return NarrativeApp(nv.build_tree(big), live=False, mouse=False, full=full)


def test_move_cursor_clamps_and_tracks_tail() -> None:
    app = _app()
    app.cursor_line = 0
    app.move_cursor(-5)
    assert app.cursor_line == 0
    app.goto_tail()
    assert app.cursor_line == len(app._lines) - 1
    assert app.follow_tail is True
    app.move_cursor(-1)
    assert app.follow_tail is False


def test_toggle_fold_at_cursor_is_fold_stable() -> None:
    app = _app()
    # Move cursor onto the thinking fold header.
    idx = [i for i, ln in enumerate(app._lines)
           if ln.is_fold_header and isinstance(ln.node, nv.ThinkingFold)][0]
    app.cursor_line = idx
    fold_node = app._lines[idx].node
    app.toggle_fold_at_cursor()
    # After expanding, the cursor still sits on that same fold header.
    assert app._lines[app.cursor_line].node is fold_node
    assert app._lines[app.cursor_line].is_fold_header
    assert app.fold_state.is_expanded(fold_node) is True


def test_next_prev_fold_moves_cursor_between_headers() -> None:
    app = _app()
    app.cursor_line = 0
    app.next_fold()
    assert app._lines[app.cursor_line].is_fold_header
    first = app.cursor_line
    app.next_fold()
    assert app.cursor_line >= first


def test_expand_all_then_collapse_all() -> None:
    app = _app()
    app.expand_all()
    assert app.fold_state.default_expanded is True
    blob = "\n".join("".join(t for _s, t in ln.fragments) for ln in app._lines)
    assert "▾" in blob
    app.collapse_all()
    assert app.fold_state.default_expanded is False
    blob2 = "\n".join("".join(t for _s, t in ln.fragments) for ln in app._lines)
    assert "▾" not in blob2

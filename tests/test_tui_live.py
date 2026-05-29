# tests/test_tui_live.py
from __future__ import annotations

from imp.cli import narrative as nv
from imp.cli.tui import NarrativeApp


def _ev(eid, etype, agent_id=None, **payload):
    return {"id": eid, "agent_id": agent_id, "type": etype, "payload": payload}


def test_on_event_grows_tree_and_keeps_cursor_pinned_to_tail() -> None:
    app = NarrativeApp(nv.build_tree([]), live=True, mouse=False)
    assert app.follow_tail is True
    app.on_event(_ev("1", "change_created", change_id=1, title="t"))
    app.on_event(_ev("2", "phase_started", phase="design"))
    app.on_event(_ev("3", "step_started", step="spec", model="m", provider="anthropic"))
    # Sticky tail: cursor stays at the last line as content streams in.
    assert app.cursor_line == len(app._lines) - 1
    blob = "\n".join("".join(t for _s, t in ln.fragments) for ln in app._lines)
    assert "design" in blob and "spec" in blob


def test_scrolled_up_user_is_not_yanked_to_tail_on_new_event() -> None:
    app = NarrativeApp(nv.build_tree([]), live=True, mouse=False)
    for e in [_ev("1", "change_created", change_id=1, title="t"),
              _ev("2", "phase_started", phase="design"),
              _ev("3", "step_started", step="spec", model="m", provider="anthropic")]:
        app.on_event(e)
    app.goto_top()                          # user scrolls up
    assert app.follow_tail is False
    pinned = app.cursor_line
    app.on_event(_ev("4", "thinking", agent_id="step:design/spec", content="x"))
    assert app.cursor_line == pinned        # viewport didn't jump
    assert app.new_below_count >= 1         # footer "↓ N new" hint source


def test_subagent_tokens_count_toward_footer_total() -> None:
    app = NarrativeApp(nv.build_tree([]), live=True, mouse=False)
    app.on_event(_ev("1", "change_created", change_id=1, title="t"))
    app.on_event(_ev("tk", "tool_call", agent_id=None, id="b1", name="task",
                     input={"subagent_name": "rev", "task_description": "go"}))
    # NB: a task tool_call at root has no open step; builder attaches at root.
    app.on_event(_ev("s", "llm_response", agent_id="sub:tk", provider="anthropic",
                     model="m", stop_reason="end_turn",
                     usage={"input_tokens": 99, "output_tokens": 9}, raw={"content": []}))
    assert app.tree.tokens.input == 99      # subagent tokens included (F2)

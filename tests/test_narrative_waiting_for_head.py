from imp.cli.narrative import NarrativeBuilder
from imp.cli.render_text import render_tree


def test_waiting_for_head_renders_position_line() -> None:
    b = NarrativeBuilder()
    b.add({"type": "waiting_for_head", "agent_id": None,
           "payload": {"base": "main", "ahead": 2}})
    out = "\n".join(render_tree(b.tree, full=False, color=False, width=80))
    assert "waiting to merge — 2 ahead" in out


def test_waiting_for_head_renders_zero_ahead() -> None:
    b = NarrativeBuilder()
    b.add({"type": "waiting_for_head", "agent_id": None,
           "payload": {"base": "main", "ahead": 0}})
    out = "\n".join(render_tree(b.tree, full=False, color=False, width=80))
    assert "waiting to merge — 0 ahead" in out


def test_waiting_to_merge_renders_lifecycle_yellow() -> None:
    from imp.cli import palette as pal
    b = NarrativeBuilder()
    b.add({"type": "waiting_for_head", "agent_id": None,
           "payload": {"base": "main", "ahead": 2}})
    out = "\n".join(render_tree(b.tree, full=False, color=True, width=80))
    assert "waiting to merge — 2 ahead" in out
    assert pal._ANSI["yellow"] in out

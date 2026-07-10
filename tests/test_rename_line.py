from __future__ import annotations

from imp.cli import narrative as nv
from imp.cli.render_text import render_tree
from tests._event_fixtures import ev


def test_change_renamed_appends_a_rename_node_with_the_title() -> None:
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title=""),
        ev("2", "change_renamed", agent_id="a", id=1, old_title="",
           new_title="Add version flag"),
    ])
    renames = [c for c in tree.root.children if isinstance(c, nv.RenameNode)]
    assert len(renames) == 1
    assert renames[0].name == "Add version flag"
    assert tree.root.title == "Add version flag"


def test_partial_rename_event_appends_no_node() -> None:
    # A legacy/partial event missing new_title leaves the transcript untouched.
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "change_renamed", agent_id="a", id=1, old_title="x"),
    ])
    assert not any(isinstance(c, nv.RenameNode) for c in tree.root.children)


def test_rename_line_renders_agent_aside_with_set_name() -> None:
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title=""),
        ev("2", "change_renamed", agent_id="a", id=1, old_title="",
           new_title="Add version flag"),
    ])
    body = list(render_tree(tree, full=False, color=False, width=80))
    assert any(ln.strip() == '• Set name to "Add version flag"' for ln in body)


def test_rename_line_color_bullet_and_name_yellow() -> None:
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title=""),
        ev("2", "change_renamed", agent_id="a", id=1, old_title="",
           new_title="Add version flag"),
    ])
    body = list(render_tree(tree, full=False, color=True, width=80))
    row = next(ln for ln in body if "Set name to" in ln)
    assert "\x1b[33m• \x1b[0m" in row                          # yellow turn bullet
    assert "\x1b[33mAdd version flag\x1b[0m" in row           # name non-bold yellow


def test_rename_line_bullet_and_name_are_wordmark_under_dark_theme() -> None:
    from imp.cli import palette as pal
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title=""),
        ev("2", "change_renamed", agent_id="a", id=1, old_title="",
           new_title="Add version flag"),
    ])
    try:
        pal.set_theme("dark")
        body = list(render_tree(tree, full=False, color=True, width=80))
    finally:
        pal.set_theme("ansi")
    row = next(ln for ln in body if "Set name to" in ln)
    wordmark = pal.theme_escapes("dark")["wordmark"]   # \x1b[38;2;255;154;60m
    yellow = pal.theme_escapes("dark")["yellow"]        # \x1b[38;2;199;151;106m
    assert wordmark + "• " in row                       # bullet painted wordmark
    assert wordmark + "Add version flag" in row         # name painted wordmark
    assert yellow not in row                             # not the muted yellow role


def test_rename_renders_after_narration_in_sdk_order() -> None:
    # SDK-path log order is [change_renamed, llm_response(prose), tool_call]; the
    # built tree must place the narration (AgentTextNode) BEFORE the rename effect
    # line (RenameNode) within their container.
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title=""),
        ev("2", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("3", "change_renamed", agent_id="step:design/spec", id=1,
           old_title="", new_title="Order the rename"),
        ev("4", "llm_response", agent_id="step:design/spec", provider="anthropic",
           model="m", stop_reason="end_turn", usage={},
           raw={"content": [{"type": "text", "text": "Naming this change now."}]}),
        ev("5", "tool_call", agent_id="step:design/spec", id="c1",
           name="rename_change", input={"name": "Order the rename"}),
        ev("6", "tool_result", agent_id="step:design/spec", id="c1",
           content="ok", error=False),
        ev("7", "step_completed", step="spec", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ])
    step = tree.root.children[0].children[0]        # phase -> step
    kids = step.children
    text_idx = next(i for i, c in enumerate(kids) if isinstance(c, nv.AgentTextNode))
    rename_idx = next(i for i, c in enumerate(kids) if isinstance(c, nv.RenameNode))
    assert text_idx < rename_idx                     # narration BEFORE rename
    assert sum(isinstance(c, nv.RenameNode) for c in kids) == 1
    assert tree.root.title == "Order the rename"      # title still applied at once


def test_rename_without_narration_flushes_at_step_boundary() -> None:
    # A rename with no following llm_response before the step ends still produces
    # exactly one RenameNode, flushed into the step at the step_completed boundary.
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title=""),
        ev("2", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("3", "change_renamed", agent_id="step:design/spec", id=1,
           old_title="", new_title="Renamed early"),
        ev("4", "step_completed", step="spec", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ])
    step = tree.root.children[0].children[0]
    renames = [c for c in step.children if isinstance(c, nv.RenameNode)]
    assert len(renames) == 1
    assert renames[0].name == "Renamed early"


def test_two_renames_before_flush_coalesce_to_latest() -> None:
    # Two change_renamed events before any flush yield a single RenameNode
    # carrying the latest title (the stash holds one; the second replaces it).
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title=""),
        ev("2", "change_renamed", agent_id="a", id=1, old_title="", new_title="First"),
        ev("3", "change_renamed", agent_id="a", id=1, old_title="First",
           new_title="Second"),
    ])
    renames = [c for c in tree.root.children if isinstance(c, nv.RenameNode)]
    assert len(renames) == 1
    assert renames[0].name == "Second"
    assert tree.root.title == "Second"

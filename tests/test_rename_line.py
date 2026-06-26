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

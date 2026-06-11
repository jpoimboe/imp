from __future__ import annotations

from imp.cli import narrative as nv
from imp.cli.narrative import _slug_descriptor
from imp.cli.render_text import render_tree
from tests._event_fixtures import ev


def test_slug_descriptor_strips_the_date_prefix() -> None:
    assert _slug_descriptor("2026-06-11-add-version-flag") == "add-version-flag"
    assert _slug_descriptor("legacy-no-date") == "legacy-no-date"
    assert _slug_descriptor("") == ""


def test_change_renamed_appends_a_rename_node_with_the_descriptor() -> None:
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title=""),
        ev("2", "change_renamed", agent_id="a", id=1, old_title="",
           new_title="Add version flag", new_slug="2026-06-11-add-version-flag"),
    ])
    renames = [c for c in tree.root.children if isinstance(c, nv.RenameNode)]
    assert len(renames) == 1
    assert renames[0].name == "add-version-flag"
    # The header fields still update (existing behavior, unchanged).
    assert tree.root.slug == "2026-06-11-add-version-flag"
    assert tree.root.title == "Add version flag"


def test_partial_rename_event_appends_no_node() -> None:
    # A legacy/partial event missing new_slug leaves the transcript untouched.
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "change_renamed", agent_id="a", id=1, new_title="Y"),
    ])
    assert not any(isinstance(c, nv.RenameNode) for c in tree.root.children)


def test_rename_line_renders_arrow_named_descriptor() -> None:
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title=""),
        ev("2", "change_renamed", agent_id="a", id=1, old_title="",
           new_title="Add version flag", new_slug="2026-06-11-add-version-flag"),
    ])
    body = list(render_tree(tree, full=False, color=False, width=80))
    assert any(ln.strip() == "→ named add-version-flag" for ln in body)


def test_rename_line_color_marker_dim_name_bold_magenta() -> None:
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title=""),
        ev("2", "change_renamed", agent_id="a", id=1, old_title="",
           new_title="Add version flag", new_slug="2026-06-11-add-version-flag"),
    ])
    body = list(render_tree(tree, full=False, color=True, width=80))
    row = next(ln for ln in body if "named" in ln)
    assert "\x1b[2m→ named \x1b[0m" in row                 # marker dim
    assert "\x1b[1m\x1b[35madd-version-flag\x1b[0m" in row  # name bold+magenta

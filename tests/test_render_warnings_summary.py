from imp.cli import narrative as nv
from imp.cli import palette as pal
from imp.cli import render_text as rt


def _root_with(children: list) -> nv.NarrativeTree:
    root = nv.WorkflowNode(id="abc123", title="x")
    root.children = children
    return nv.NarrativeTree(root=root, tokens=nv.TokenTotals())


def _render(children: list, *, color: bool = False) -> str:
    tree = _root_with(children)
    return "\n".join(rt.render_tree(tree, full=False, color=color, width=80))


def test_summary_header_uses_total_and_pluralizes() -> None:
    node = nv.WarningsSummaryNode(lines=[("alpha", 2), ("beta", 1)], total=3)
    out = _render([node])
    assert "3 warnings this run" in out


def test_summary_header_singular_for_one_warning() -> None:
    node = nv.WarningsSummaryNode(lines=[("alpha", 1)], total=1)
    out = _render([node])
    assert "1 warning this run" in out
    assert "warnings" not in out


def test_summary_bullets_show_count_only_on_repeats_in_first_seen_order() -> None:
    node = nv.WarningsSummaryNode(lines=[("alpha", 2), ("beta", 1)], total=3)
    out = _render([node]).splitlines()
    body = [ln.strip() for ln in out if ln.strip()]
    assert body == ["3 warnings this run", "• alpha  (×2)", "• beta"]


def test_summary_rendered_in_warning_palette_when_colored() -> None:
    node = nv.WarningsSummaryNode(lines=[("alpha", 1)], total=1)
    out = _render([node], color=True)
    assert pal.sgr("wordmark", enabled=True) in out  # NODE_COLORS["warning"]


def test_completed_node_renders_completed_id_line() -> None:
    out = _render([nv.CompletedNode(quest_id="abc123")], color=True)
    assert "✓ Completed abc123." in out
    assert pal.sgr("wordmark", enabled=True) in out  # NODE_COLORS["lifecycle"]


def test_completed_node_plain_has_no_glyph() -> None:
    out = _render([nv.CompletedNode(quest_id="abc123")], color=False)
    assert "Completed abc123." in out
    assert "✓" not in out


def test_summary_bullet_caret_escapes_control_chars() -> None:
    node = nv.WarningsSummaryNode(lines=[("bad\x07msg", 1)], total=1)
    out = _render([node])
    assert "bad^Gmsg" in out
    assert "\x07" not in out

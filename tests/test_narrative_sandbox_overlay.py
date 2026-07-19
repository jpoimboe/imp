from __future__ import annotations

from imp.cli import narrative as nv
from imp.cli.narrative import NarrativeBuilder
from imp.cli.render_text import render_tree

_REASON = ("sandbox: unprivileged overlayfs unavailable — $HOME is a writable "
           "bind of your real home, so agent writes to $HOME persist (secrets "
           "are still masked)")

_EVENT = {"type": "sandbox_overlay_unavailable", "payload": {"reason": _REASON}}


def _nodes(events: list[dict], cls: type) -> list:
    nb = NarrativeBuilder()
    for e in events:
        nb.add(e)
    found: list = []

    def _walk(node: object) -> None:
        if isinstance(node, cls):
            found.append(node)
        for child in getattr(node, "children", []):
            _walk(child)

    _walk(nb.tree.root)
    return found


def test_handler_registered() -> None:
    assert "sandbox_overlay_unavailable" in NarrativeBuilder._HANDLERS


def test_event_yields_visible_warning_node_and_no_fallback() -> None:
    errors = _nodes([_EVENT], nv.ErrorNode)
    assert len(errors) == 1
    assert errors[0].is_warning is True
    assert errors[0].message == _REASON
    # No longer a suppressed FallbackLine.
    assert _nodes([_EVENT], nv.FallbackLine) == []


def test_event_tallies_into_warnings_summary() -> None:
    nb = NarrativeBuilder()
    nb.add(_EVENT)
    assert _REASON in nb._warnings


def test_event_renders_warning_line_without_full() -> None:
    tree = nv.build_tree([_EVENT])
    out = "\n".join(render_tree(tree, full=False, color=False, width=80))
    assert f"warning: {_REASON}" in out


def test_missing_reason_falls_back_to_placeholder() -> None:
    errors = _nodes(
        [{"type": "sandbox_overlay_unavailable", "payload": {}}], nv.ErrorNode)
    assert len(errors) == 1
    assert errors[0].message == "?"


def test_unrelated_unknown_type_still_falls_back() -> None:
    fallbacks = _nodes(
        [{"type": "widget_frobbed", "payload": {"x": 1}}], nv.FallbackLine)
    assert len(fallbacks) == 1
    assert fallbacks[0].event_type == "widget_frobbed"

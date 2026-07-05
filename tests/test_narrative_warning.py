from __future__ import annotations

from imp.cli import narrative as nv
from imp.cli.narrative import NarrativeBuilder
from imp.cli.render_text import render_tree


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


def test_warning_handler_registered() -> None:
    assert "warning" in NarrativeBuilder._HANDLERS


def test_warning_appends_muted_error_node() -> None:
    errors = _nodes([
        {"type": "warning",
         "payload": {"source": "artifact_harvest",
                     "message": ("artifact ref refs/imp/artifacts/x unreadable; "
                                 "starting new artifact lineage"),
                     "detail": {}}},
    ], nv.ErrorNode)
    assert len(errors) == 1
    assert errors[0].is_warning is True
    assert "starting new artifact lineage" in errors[0].message


def test_warning_renders_muted_line() -> None:
    tree = nv.build_tree([
        {"type": "warning",
         "payload": {"source": "artifact_harvest",
                     "message": "harvest skipped: git fatal", "detail": {}}},
    ])
    out = "\n".join(render_tree(tree, full=False, color=False, width=80))
    assert "warning: harvest skipped: git fatal" in out


def test_warning_tallies_into_summary() -> None:
    nb = NarrativeBuilder()
    nb.add({"type": "warning",
            "payload": {"source": "artifact_harvest",
                        "message": "harvest skipped: git fatal", "detail": {}}})
    assert "harvest skipped: git fatal" in nb._warnings


def test_unrelated_unknown_type_still_falls_back() -> None:
    # Adding the `warning` handler must not change how an unrelated unknown type
    # is handled: it still renders as a FallbackLine, not an ErrorNode.
    fallbacks = _nodes(
        [{"type": "widget_frobbed", "payload": {"x": 1}}], nv.FallbackLine)
    assert len(fallbacks) == 1
    assert fallbacks[0].event_type == "widget_frobbed"

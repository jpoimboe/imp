"""docs/internals/knowledge-map.md — the self-knowledge layout convention."""

from __future__ import annotations

from pathlib import Path

DOC = Path("docs/internals/knowledge-map.md")


def test_knowledge_map_exists_and_is_nonempty() -> None:
    assert DOC.is_file()
    assert DOC.read_text().strip()


def test_knowledge_map_states_the_portability_boundary() -> None:
    low = DOC.read_text().lower()
    assert "portab" in low
    # skills ship in the package; the corpus is resolved in place, not copied
    assert "in place" in low
    assert "{{ docs.path }}" in DOC.read_text()
    # skills, by contrast, ARE overlaid into each per-quest snapshot
    assert "overlaid" in low or "overlay" in low


def test_knowledge_map_names_the_three_layers_and_routing_rule() -> None:
    low = DOC.read_text().lower()
    assert "three" in low
    # the three layers
    assert "skill" in low
    assert "corpus" in low
    assert "workflow prompt" in low
    # the routing rule keywords
    assert "descriptive" in low
    assert "step-specific" in low


def test_knowledge_map_states_the_thin_index_pattern() -> None:
    assert "thin index" in DOC.read_text().lower()

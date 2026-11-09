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


def test_knowledge_map_names_the_shared_partials_overlay() -> None:
    text = DOC.read_text()
    low = text.lower()
    # workflow-agnostic partials ship bundled and are overlaid like skills
    assert "src/imp/assets/partials/" in text
    assert "shared partial" in low
    # the overlay is local-first, into each quest's workflow/partials/
    assert "workflow/partials/" in text
    assert "local-first" in low


def test_knowledge_map_names_where_each_install_reads_the_corpus() -> None:
    text = DOC.read_text()
    # wheels bundle the corpus as imp/assets/docs; a clone reads the repo's docs/
    assert "imp/assets/docs" in text
    assert "does not require bundling" not in text

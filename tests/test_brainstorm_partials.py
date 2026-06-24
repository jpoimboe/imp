from __future__ import annotations

from pathlib import Path

PARTIALS = Path("src/imp/assets/workflows/imp/partials")


def test_spec_design_principles_carries_judgment_lens_only() -> None:
    text = (PARTIALS / "spec-design-principles.md").read_text()
    # design-for-clarity
    assert "do not propose unrelated refactoring" in text
    # cover-the-design (the concrete coverage list)
    assert "architecture, components, data flow, error handling, and testing" in text
    # YAGNI
    assert "YAGNI ruthlessly" in text
    # It must NOT carry dialogue-conduct prose (that lives in brainstorm-dialogue).
    assert "One question at a time" not in text
    assert "Propose 2" not in text
    assert "build this out now" not in text.lower()
    # No superpowers attribution line.
    assert "superpowers" not in text.lower()

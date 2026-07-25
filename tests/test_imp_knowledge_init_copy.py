from __future__ import annotations

from pathlib import Path

_SKILL = Path("src/imp/assets/skills/imp-knowledge.md")


def test_imp_knowledge_reflects_conversational_init() -> None:
    text = _SKILL.read_text()
    # The retired one-shot import-root detection wording is gone.
    assert "one-shot agent" not in text
    # The new provider-free non-interactive flag is documented.
    assert "--use-defaults" in text

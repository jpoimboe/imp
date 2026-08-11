from __future__ import annotations

from pathlib import Path

from imp.cli import init as init_mod

_SKILL = Path("src/imp/assets/skills/imp-knowledge.md")
_README = Path("README.md")
_INIT_PROSE = (_SKILL, _README, Path("docs/guide/memory.md"))


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_imp_knowledge_documents_the_non_interactive_flag() -> None:
    text = _SKILL.read_text()
    # The retired one-shot import-root detection wording is gone.
    assert "one-shot agent" not in text
    # The provider-free non-interactive flag is documented.
    assert "--use-defaults" in text


def test_init_prose_describes_a_detected_and_confirmed_setup() -> None:
    for path in _INIT_PROSE:
        flat = _flat(path.read_text())
        # README's pinned launch copy says "confirms on one screen"; the skill and
        # the memory doc keep the full phrase.
        phrase = "on one screen" if path == _README else "confirms them on one screen"
        assert phrase in flat, path
        assert "interactive setup" not in flat, path
        assert "interactive conversation" not in flat, path
        assert "skip the conversation" not in flat, path


def test_init_module_docstring_needs_no_provider() -> None:
    doc = _flat(init_mod.__doc__ or "")
    assert "confirms them on one screen" in doc
    assert "needs no model provider" in doc
    assert "needs a working provider" not in doc

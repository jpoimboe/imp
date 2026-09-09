from __future__ import annotations

from pathlib import Path

from tests._corpus import tracked

_SKILL = Path("src/imp/assets/skills/imp-knowledge.md")
_PLAYBOOK = Path("tests/playbooks/imp-ux-playbook.md")
# Shipped agent-facing prose only; an untracked scratch note under docs/ is not.
_AGENT_DOC_ROOTS = ("docs", "src/imp/assets/skills", "tests/playbooks")


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_imp_knowledge_names_the_provider_flag_and_slash_command() -> None:
    flat = _flat(_SKILL.read_text())
    assert "`--provider NAME` on `imp new` / `imp chat` / `imp resume`" in flat
    assert "`/provider`" in flat


def test_no_agent_doc_mentions_the_removed_model_flag() -> None:
    offenders = [str(path) for path in tracked(*_AGENT_DOC_ROOTS)
                 if "--model" in path.read_text()]
    assert offenders == []


def test_ux_playbook_carries_provider_in_both_stale_spots() -> None:
    flat = _flat(_PLAYBOOK.read_text())
    assert "`--base`, `--provider`)" in flat            # imp new's flag surface
    assert "`/sandbox [on|off]`, `/provider [name]`," in flat   # the slash set

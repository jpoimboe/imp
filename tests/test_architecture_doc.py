"""docs/architecture.md (a living doc) names the renamed artifact ref and no
longer mentions the deleted journal-wip ref or the agent-commits-via-CLI command."""

from __future__ import annotations

from pathlib import Path

ARCH = Path("docs/architecture.md")


def test_architecture_uses_renamed_artifact_ref() -> None:
    text = ARCH.read_text()
    assert "journal-wip" not in text
    assert "refs/imp/artifacts/<id>" in text


def test_architecture_drops_artifact_commit_command() -> None:
    assert "artifact-commit" not in ARCH.read_text()


def test_architecture_names_quest_concept() -> None:
    text = ARCH.read_text()
    assert "| Quest / state | `engine/quest.py`, `engine/state.py` |" in text
    assert "A single **Quest** is one unit of work" in text
    assert "the last plan of a multi-plan Quest" in text
    assert "engine/change.py" not in text
    assert "| Change / state |" not in text
    assert ".imp/change/" not in text
    assert "multi-plan change" not in text


def test_what_is_imp_names_quest_concept() -> None:
    text = Path("docs/what-is-imp.md").read_text()
    assert "The core idea is a **Quest**" in text
    assert "per-quest home on disk." in text
    assert "**Change**" not in text

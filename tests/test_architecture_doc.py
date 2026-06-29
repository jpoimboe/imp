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

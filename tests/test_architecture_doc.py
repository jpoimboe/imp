"""docs/internals/architecture.md (a living doc) names the renamed artifact ref and no
longer mentions the deleted journal-wip ref or the agent-commits-via-CLI command."""

from __future__ import annotations

from pathlib import Path

ARCH = Path("docs/internals/architecture.md")


def test_architecture_uses_renamed_artifact_ref() -> None:
    text = ARCH.read_text()
    assert "journal-wip" not in text
    assert "refs/imp/artifacts/<id>" in text


def test_architecture_drops_artifact_commit_command() -> None:
    assert "artifact-commit" not in ARCH.read_text()


def test_architecture_names_quest_concept() -> None:
    text = ARCH.read_text()
    assert "| Quest / state | `engine/quest.py`, `engine/state.py` |" in text
    assert "A single **quest** is one unit of work" in text
    assert "the last plan of a multi-plan quest" in text
    assert "engine/change.py" not in text
    assert "| Change / state |" not in text
    assert ".imp/change/" not in text
    assert "multi-plan change" not in text


def test_what_is_imp_names_quest_concept() -> None:
    text = Path("docs/guide/what-is-imp.md").read_text()
    assert "The core idea is a **quest**" in text
    assert "per-quest home on disk." in text
    assert "**Change**" not in text


def test_imp_conventions_names_quest_concept() -> None:
    text = Path("docs/internals/conventions.md").read_text()
    assert "nouns: quest, workflow, backlog, phase," in text
    assert "nouns: Change," not in text


def test_imp_invariants_names_quest_concept() -> None:
    text = Path("docs/internals/invariants.md").read_text()
    assert "quest.json" in text
    assert "change.json" not in text
    assert "change_id" not in text
    assert ".imp/changes/" not in text


def test_imp_operator_notes_names_quest_concept() -> None:
    text = Path("docs/guide/operator-notes.md").read_text()
    assert "Runbook for operating a running imp quest." in text
    assert ".imp/quest/workflow" in text
    assert ".imp/change/" not in text
    assert ".imp/changes/" not in text


def test_docs_reorg_moved_all_eight_with_no_stale_root_copy() -> None:
    moved = {
        "docs/what-is-imp.md": "docs/guide/what-is-imp.md",
        "docs/imp-operator-notes.md": "docs/guide/operator-notes.md",
        "docs/architecture.md": "docs/internals/architecture.md",
        "docs/imp-conventions.md": "docs/internals/conventions.md",
        "docs/imp-invariants.md": "docs/internals/invariants.md",
        "docs/subagent-authoring.md": "docs/internals/subagent-authoring.md",
        "docs/tdd-analysis-report.md": "docs/internals/tdd-analysis-report.md",
        "docs/tdd-brittleness-verdict.md": "docs/internals/tdd-brittleness-verdict.md",
    }
    for old, new in moved.items():
        assert not Path(old).exists(), f"stale root copy left: {old}"
        assert Path(new).is_file(), f"missing at new home: {new}"

from __future__ import annotations

import json
from pathlib import Path

from imp.engine.quest import Quest
from tests.conftest import _seed_control


def _meta(quest: Quest) -> dict:
    return json.loads((quest.quest_dir / "quest.json").read_text())


def test_create_draft_persists_the_pin_it_was_given(tmp_path: Path) -> None:
    _seed_control(tmp_path)

    quest = Quest.create_draft(
        tmp_path, title="x", quest_id="pin01", base_branch="main",
        provider="codex")

    assert quest.provider == "codex"
    assert _meta(quest)["provider"] == "codex"
    assert Quest.load(tmp_path, "pin01").provider == "codex"


def test_create_draft_without_a_pin_records_an_explicit_null(tmp_path: Path) -> None:
    # The key is PRESENT and null, not absent: every v19 home has the same shape,
    # whether it was created fresh or migrated.
    _seed_control(tmp_path)

    quest = Quest.create_draft(
        tmp_path, title="x", quest_id="pin02", base_branch="main")

    assert quest.provider is None
    assert "provider" in _meta(quest)
    assert _meta(quest)["provider"] is None
    assert Quest.load(tmp_path, "pin02").provider is None


def test_set_provider_writes_disk_and_the_live_instance(tmp_path: Path) -> None:
    # Arming is the durable act: one call makes the pin visible to the engine
    # instance in memory AND to any other process, at the same instant.
    _seed_control(tmp_path)
    quest = Quest.create_draft(
        tmp_path, title="x", quest_id="pin03", base_branch="main")

    quest.set_provider("codex")

    assert quest.provider == "codex"
    assert _meta(quest)["provider"] == "codex"
    assert _meta(quest)["title"] == "x"          # the rewrite preserves the rest


def test_set_provider_can_clear_the_pin(tmp_path: Path) -> None:
    # None must round-trip as a value, not a no-op: clearing an unusable pin is a real operation.
    _seed_control(tmp_path)
    quest = Quest.create_draft(
        tmp_path, title="x", quest_id="pin04", base_branch="main",
        provider="codex")

    quest.set_provider(None)

    assert quest.provider is None
    assert _meta(quest)["provider"] is None
    assert Quest.load(tmp_path, "pin04").provider is None

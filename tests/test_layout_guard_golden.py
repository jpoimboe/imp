from __future__ import annotations

import json
import os
from pathlib import Path

from imp.engine.quest import LAYOUT_VERSION, Quest
from tests.conftest import _seed_control

_GOLDEN = Path(__file__).parent / "fixtures" / "layout_guard_golden.txt"


def _observe_layout(quest_dir: Path) -> str:
    """Sorted on-disk paths + key-sets of quest.json and state.json; values excluded."""
    files = sorted(
        str(p.relative_to(quest_dir)).replace(os.sep, "/")
        for p in quest_dir.rglob("*") if p.is_file()
    )
    change_keys = sorted(json.loads((quest_dir / "quest.json").read_text()))
    state_keys = sorted(json.loads((quest_dir / "state" / "state.json").read_text()))
    lines = ["[paths]", *files,
             "[quest.json]", *change_keys,
             "[state.json]", *state_keys]
    return "\n".join(lines) + "\n"


def test_observed_layout_matches_golden_and_version_is_paired(tmp_path: Path) -> None:
    header, _, body = _GOLDEN.read_text().partition("\n")
    golden_version = int(header.split("LAYOUT_VERSION:")[1].strip())

    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="X", quest_id="golden", base_branch="main")
    observed = _observe_layout(ch.quest_dir)

    # structural drift fails until golden body and LAYOUT_VERSION constant are updated together
    assert observed == body
    assert golden_version == LAYOUT_VERSION

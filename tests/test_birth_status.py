from __future__ import annotations

import json
from pathlib import Path

from imp.engine.change import Change
from tests.conftest import _seed_control


def _persisted_status(change: Change) -> str:
    # Read the raw JSON, NOT State.load — load coerces legacy values, which would
    # mask a regression that wrote "running".
    return json.loads((change.state_dir / "state.json").read_text())["status"]


def test_create_persists_new(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", change_id="brnnew", base_branch="main")
    assert _persisted_status(ch) == "new"


def test_create_draft_persists_new(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="X", change_id="brndrf", base_branch="main")
    assert _persisted_status(ch) == "new"

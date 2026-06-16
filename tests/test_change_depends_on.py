from __future__ import annotations

import json
from pathlib import Path

from imp.engine.change import Change
from tests.conftest import _seed_control


def test_create_writes_depends_on(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", change_id="2026-06-16-ccc111",
                       base_branch="main", depends_on=["2026-06-16-dep001"])
    meta = json.loads((ch.change_dir / "change.json").read_text())
    assert meta["depends_on"] == ["2026-06-16-dep001"]


def test_create_defaults_depends_on_empty(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", change_id="2026-06-16-ccc222",
                       base_branch="main")
    meta = json.loads((ch.change_dir / "change.json").read_text())
    assert meta["depends_on"] == []

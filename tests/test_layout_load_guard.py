from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.engine.change import LAYOUT_VERSION, Change, migrate
from imp.engine.errors import LayoutMismatch, LoadError
from tests.conftest import _seed_control


def _set_stored_version(quest_dir: Path, version: int) -> None:
    meta_path = quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = version
    meta_path.write_text(json.dumps(meta))


def test_migrate_always_raises_while_registry_empty(tmp_path: Path) -> None:
    (tmp_path / "quest.json").write_text(json.dumps({"imp_commit": "deadbeef"}))
    with pytest.raises(LayoutMismatch):
        migrate(0, LAYOUT_VERSION, tmp_path)
    with pytest.raises(LayoutMismatch):
        migrate(LAYOUT_VERSION + 7, LAYOUT_VERSION, tmp_path)


def test_load_refuses_lower_stored_version(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", quest_id="lo", base_branch="main")
    _set_stored_version(ch.quest_dir, 0)        # no (0, 2) migration -> refuse
    with pytest.raises(LayoutMismatch):
        Change.load(tmp_path, "lo")


def test_load_refuses_higher_stored_version(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", quest_id="hi", base_branch="main")
    _set_stored_version(ch.quest_dir, LAYOUT_VERSION + 999)
    with pytest.raises(LayoutMismatch):
        Change.load(tmp_path, "hi")


def test_load_refuses_when_version_key_absent(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", quest_id="old", base_branch="main")
    meta_path = ch.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    del meta["layout_version"]                 # a pre-guard quest.json -> defaults to 0
    meta_path.write_text(json.dumps(meta))
    with pytest.raises(LayoutMismatch):
        Change.load(tmp_path, "old")


def test_freshly_created_change_loads_without_raising(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="X", quest_id="ok", base_branch="main")
    loaded = Change.load(tmp_path, "ok")
    assert loaded.id == "ok"


def test_home_absent_raises_plain_loaderror_not_layoutmismatch(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    with pytest.raises(LoadError) as ei:
        Change.load(tmp_path, "no-such-change")
    assert not isinstance(ei.value, LayoutMismatch)

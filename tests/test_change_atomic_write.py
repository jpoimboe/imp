from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from imp.engine.change import (
    Change,
    _migrate_v4_to_v5,
    _write_change_json,
)
from tests.conftest import _seed_control


def _boom(*_a, **_k):
    raise OSError("simulated crash before os.replace")


def test_write_change_json_output_is_byte_identical(tmp_path: Path) -> None:
    # Keys deliberately out of order + nested to pin sort_keys=True and indent=2.
    meta = {"b": 2, "a": 1, "nested": {"y": [3, 2, 1], "x": "v"}}
    meta_path = tmp_path / "change.json"
    _write_change_json(meta_path, meta)
    assert meta_path.read_text() == json.dumps(meta, indent=2, sort_keys=True)


def test_write_change_json_replaces_existing_content(tmp_path: Path) -> None:
    meta_path = tmp_path / "change.json"
    meta_path.write_text(json.dumps({"layout_version": 1}, indent=2, sort_keys=True))
    _write_change_json(meta_path, {"layout_version": 2, "title": "renamed"})
    reloaded = json.loads(meta_path.read_text())
    assert reloaded == {"layout_version": 2, "title": "renamed"}
    assert not list(tmp_path.glob(".change-*"))  # no temp file left behind


def test_write_change_json_preserves_original_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    meta_path = tmp_path / "change.json"
    original = json.dumps({"layout_version": 1}, indent=2, sort_keys=True)
    meta_path.write_text(original)
    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        _write_change_json(meta_path, {"layout_version": 2})
    # Live file untouched, and the transient temp file was cleaned up.
    assert meta_path.read_text() == original
    assert not list(tmp_path.glob(".change-*"))


def test_migration_advances_version_and_leaves_no_temp(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", quest_id="mig01", base_branch="main")
    meta_path = ch.change_dir / "change.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 4                       # pretend it was written by v4
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))

    _migrate_v4_to_v5(ch.change_dir)

    reloaded = json.loads(meta_path.read_text())     # still valid JSON
    assert reloaded["layout_version"] == 5
    assert not list(ch.change_dir.glob(".change-*"))


def test_rename_setter_preserves_change_json_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="Original", quest_id="atom01",
                       base_branch="main")
    meta_path = ch.change_dir / "change.json"
    original = meta_path.read_text()
    assert '"title": "Original"' in original

    # Patch AFTER create/state seeding so only the rename write is intercepted.
    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        ch.rename("Renamed", log=ch.event_log())

    # The setter routed through the atomic helper: os.replace raised, so the
    # live change.json is byte-for-byte the pre-rename content and no temp leaks.
    assert meta_path.read_text() == original
    assert "Renamed" not in meta_path.read_text()
    assert not list(ch.change_dir.glob(".change-*"))


def test_migration_preserves_change_json_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", quest_id="mig02", base_branch="main")
    meta_path = ch.change_dir / "change.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 4
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    original = meta_path.read_text()

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        _migrate_v4_to_v5(ch.change_dir)

    # Routed through the atomic helper: os.replace raised, so the live file is
    # unchanged (still layout_version 4) and no temp file leaks.
    assert meta_path.read_text() == original
    assert json.loads(meta_path.read_text())["layout_version"] == 4
    assert not list(ch.change_dir.glob(".change-*"))

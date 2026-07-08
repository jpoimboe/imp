from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from imp.engine.change import _write_change_json


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

from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.engine import durable
from imp.engine.quest import LAYOUT_VERSION, Quest, _imp_commit, _write_quest_json
from tests.conftest import _git, _seed_control


def test_layout_version_constant_is_twenty_two() -> None:
    assert LAYOUT_VERSION == 22


def test_quest_metadata_publish_fsyncs_the_containing_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meta_path = tmp_path / "quest.json"
    calls: list[str] = []
    real_fsync_file = durable._fsync_file
    real_replace = durable.os.replace
    real_fsync_directory = durable.fsync_directory

    def record_file_fsync(stream: object, path: Path) -> None:
        calls.append(f"file-fsync {meta_path.name}")
        real_fsync_file(stream, path)

    def record_replace(src: Path, dest: Path) -> None:
        calls.append(f"replace {Path(dest).name}")
        real_replace(src, dest)

    def record_directory_fsync(path: Path) -> None:
        calls.append(f"dir-fsync {path.name}")
        real_fsync_directory(path)

    monkeypatch.setattr(durable, "_fsync_file", record_file_fsync)
    monkeypatch.setattr(durable.os, "replace", record_replace)
    monkeypatch.setattr(durable, "fsync_directory", record_directory_fsync)

    _write_quest_json(meta_path, {"layout_version": 20})

    assert json.loads(meta_path.read_text()) == {"layout_version": 20}
    assert calls == [
        "file-fsync quest.json",
        "replace quest.json",
        f"dir-fsync {tmp_path.name}",
    ]



def test_create_stamps_version_and_imp_commit(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    ch = Quest.create(tmp_path, title="X", quest_id="lv1", base_branch="main")
    meta = json.loads((ch.quest_dir / "quest.json").read_text())
    assert meta["layout_version"] == LAYOUT_VERSION
    assert meta["imp_commit"] == head


def test_create_draft_stamps_version_and_imp_commit(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    ch = Quest.create_draft(tmp_path, title="X", quest_id="lv2", base_branch="main")
    meta = json.loads((ch.quest_dir / "quest.json").read_text())
    assert meta["layout_version"] == LAYOUT_VERSION
    assert meta["imp_commit"] == head


def test_imp_commit_is_empty_when_not_a_git_repo(tmp_path: Path) -> None:
    # tmp_path is not a git repository: git rev-parse HEAD fails, helper yields "".
    assert _imp_commit(tmp_path) == ""

"""The one-shot 13 -> 14 upgrade: backfill `commits` onto archived terminal events."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from imp.engine import archive
from imp.engine.archive import index, record
from imp.engine.archive import read as archive_read
from imp.engine.archive.blobstore import LocalBlobStore
from imp.engine.errors import ArchiveMismatch

QID = "mrg001"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def _repo_with_merge(root: Path) -> str:
    """A repo whose HEAD is a --no-ff merge of one feature commit; returns its sha."""
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")
    _git(root, "checkout", "-q", "-b", f"imp/{QID}")
    (root / "feature.txt").write_text("hi\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "feature work")
    _git(root, "checkout", "-q", "main")
    _git(root, "merge", "--no-ff", "-q", "-m", "Merge the feature", f"imp/{QID}")
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _terminal(event_type: str, payload: dict) -> dict:
    return {"id": "t1", "parent_id": None, "agent_id": None,
            "timestamp": "2026-06-01T00:00:09+00:00",
            "type": event_type, "payload": payload}


def _user_version(control: Path) -> int:
    conn = sqlite3.connect(archive.index_path(control))
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _seed_v13_archive(control: Path, quest_id: str, events: list[dict]) -> str:
    """An archive exactly as a v13 install left it. Built directly rather than
    through archive_quest, which would stamp the current markers."""
    store = LocalBlobStore(archive.blobs_root(control))
    events_hash = store.put(
        "".join(json.dumps(e) + "\n" for e in events).encode("utf-8"))
    rec = record.build_record(
        {"id": quest_id, "title": quest_id.title(), "base_branch": "main",
         "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []},
        {"id": quest_id, "status": "merged", "step": "finalize"},
        events_hash)
    record.write_record(archive.records_root(control), rec)
    archive.rebuild_index(control)
    conn = sqlite3.connect(archive.index_path(control))
    try:
        conn.execute("PRAGMA user_version = 6")      # the index floor v13 shipped
        conn.commit()
    finally:
        conn.close()
    archive._write_data_version(control, 13)
    return events_hash


def _archived_events(control: Path, quest_id: str) -> list[dict]:
    """read_events checks only the data floor, so this is also the bypass path."""
    raw = archive_read.read_events(control, quest_id)
    assert raw is not None
    return [json.loads(ln) for ln in raw.decode().splitlines() if ln.strip()]


def _events_hash(control: Path, quest_id: str) -> str:
    rec = record.read_record(archive.records_root(control), quest_id)
    assert rec is not None
    return str(rec["events_hash"])


def test_a_merged_payload_gains_the_merge_sides_commits(tmp_path: Path) -> None:
    merge_sha = _repo_with_merge(tmp_path)
    old_hash = _seed_v13_archive(tmp_path, QID, [
        _terminal("quest_merged", {"base": "main", "commit": merge_sha})])

    events = _archived_events(tmp_path, QID)

    payload = events[-1]["payload"]
    assert [c["subject"] for c in payload["commits"]] == ["feature work"]
    assert all(len(c["sha"]) == 40 for c in payload["commits"])
    assert merge_sha not in [c["sha"] for c in payload["commits"]]
    assert payload["base"] == "main"          # untouched keys survive the rewrite
    assert payload["commit"] == merge_sha
    assert archive.data_version_path(tmp_path).read_text().strip() == "14"
    assert _events_hash(tmp_path, QID) != old_hash     # blob rewritten, record repointed


def test_a_completed_payload_migrates_to_an_empty_list(tmp_path: Path) -> None:
    _repo_with_merge(tmp_path)
    _seed_v13_archive(tmp_path, "cmp001", [_terminal("workflow_completed", {})])

    events = _archived_events(tmp_path, "cmp001")

    # no merge sha to resolve and imp/<id> is long gone: [] is the whole answer
    assert events[-1]["payload"] == {"commits": []}


def test_an_unresolvable_merge_sha_migrates_to_an_empty_list(tmp_path: Path) -> None:
    _repo_with_merge(tmp_path)
    _seed_v13_archive(tmp_path, "gon001", [
        _terminal("quest_merged", {"base": "main", "commit": "deadbeef" * 5})])

    events = _archived_events(tmp_path, "gon001")

    assert events[-1]["payload"]["commits"] == []
    assert archive.data_version_path(tmp_path).read_text().strip() == "14"


def test_a_record_with_no_terminal_event_is_left_untouched(tmp_path: Path) -> None:
    _repo_with_merge(tmp_path)
    old_hash = _seed_v13_archive(tmp_path, "shl001", [
        {"id": "u1", "parent_id": None, "agent_id": None,
         "timestamp": "2026-06-01T00:00:01+00:00", "type": "user_message",
         "payload": {"role": "user", "subtype": "intent",
                     "content": [{"type": "text", "text": "hi"}]}}])

    archive_read.read_events(tmp_path, "shl001")

    assert _events_hash(tmp_path, "shl001") == old_hash   # blob not rewritten
    assert archive.data_version_path(tmp_path).read_text().strip() == "14"


def test_any_other_below_floor_version_still_raises(tmp_path: Path) -> None:
    _repo_with_merge(tmp_path)
    _seed_v13_archive(tmp_path, QID, [_terminal("workflow_completed", {})])
    archive._write_data_version(tmp_path, 12)

    with pytest.raises(ArchiveMismatch) as excinfo:
        archive_read.read_events(tmp_path, QID)

    assert (excinfo.value.floor, excinfo.value.stored) == ("data", 12)
    assert archive.data_version_path(tmp_path).read_text().strip() == "12"


def test_a_second_pass_does_no_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    merge_sha = _repo_with_merge(tmp_path)
    _seed_v13_archive(tmp_path, QID, [
        _terminal("quest_merged", {"base": "main", "commit": merge_sha})])
    archive_read.read_events(tmp_path, QID)
    first_hash = _events_hash(tmp_path, QID)

    calls: list[int] = []
    monkeypatch.setattr(archive, "rebuild_index", lambda _root: calls.append(1))
    archive_read.read_events(tmp_path, QID)

    assert calls == []                               # marker current -> fast path
    assert _events_hash(tmp_path, QID) == first_hash  # blob not rewritten twice


def test_a_read_only_archive_is_left_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    merge_sha = _repo_with_merge(tmp_path)
    _seed_v13_archive(tmp_path, QID, [
        _terminal("quest_merged", {"base": "main", "commit": merge_sha})])
    # The runner may be root, where mode bits deny nothing: deny the probe itself.
    real_access = os.access
    target = str(archive.archive_root(tmp_path))
    monkeypatch.setattr(
        archive.os, "access",
        lambda p, m: False if (m == os.W_OK and str(p) == target)
        else real_access(p, m))

    with pytest.raises(ArchiveMismatch) as excinfo:
        archive_read.read_events(tmp_path, QID)

    assert (excinfo.value.floor, excinfo.value.stored) == ("data", 13)
    assert archive.data_version_path(tmp_path).read_text().strip() == "13"


def test_both_markers_bump_and_the_migrated_subject_is_searchable(
    tmp_path: Path,
) -> None:
    merge_sha = _repo_with_merge(tmp_path)
    _seed_v13_archive(tmp_path, QID, [
        _terminal("quest_merged", {"base": "main", "commit": merge_sha})])
    assert _user_version(tmp_path) == 6       # falsifiable: stale before the upgrade
    assert archive_read.search_archive_events_content(tmp_path, "feature work") == []

    archive_read.read_events(tmp_path, QID)

    assert index._SCHEMA_VERSION == 7
    assert _user_version(tmp_path) == index._SCHEMA_VERSION
    assert archive.data_version_path(tmp_path).read_text().strip() == "14"
    hits = archive_read.search_archive_events_content(tmp_path, "feature work")
    assert [row[0] for row in hits] == [QID]

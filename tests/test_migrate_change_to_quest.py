from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from imp.engine.archive import index, index_path
from imp.engine.archive.read import read_quest_json

_REPO = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO / "scripts" / "migrate_change_to_quest.py"


def _seed_old_imp(root: Path) -> None:
    """Build a pre-migration .imp/ in the OLD Change-named layout:
    two live homes with change.json, two archive records keyed change_meta
    (bbb222 depends on aaa111), a populated merge-queue.json keyed change_id,
    and a valid-but-empty index.db (so a skipped rebuild is detectable)."""
    imp = root / ".imp"
    for cid, title in (("aaa111", "First Quest"), ("bbb222", "Second Quest")):
        home = imp / "changes" / cid
        home.mkdir(parents=True)
        (home / "change.json").write_text(json.dumps({
            "id": cid, "title": title, "base_branch": "main",
            "created_at": "2026-01-01T00:00:00+00:00",
            "depends_on": [], "materialized": True,
        }))
    records = imp / "archive" / "records"
    records.mkdir(parents=True)
    (imp / "archive" / "blobs").mkdir(parents=True)
    for cid, title, deps in (
        ("aaa111", "First Quest", []),
        ("bbb222", "Second Quest", ["aaa111"]),
    ):
        meta = {"id": cid, "title": title, "base_branch": "main",
                "created_at": "2026-01-01T00:00:00+00:00",
                "depends_on": deps, "materialized": True}
        (records / f"{cid}.json").write_text(json.dumps({
            "id": cid, "title": title, "base": "main",
            "created_at": "2026-01-01T00:00:00+00:00", "depends_on": deps,
            "change_meta": meta, "resumable": False, "final_state": {},
            "events_hash": "deadbeef", "step_blobs": {},
        }))
    # A valid new-schema but EMPTY index.db: if the migration skips the rebuild
    # this stays empty (no quests rows), so the post-run assertion is falsifiable.
    index.connect(index_path(root)).close()
    mq = root / ".git" / "imp" / "merge-queue.json"
    mq.parent.mkdir(parents=True)
    mq.write_text(json.dumps([
        {"change_id": "bbb222", "base_branch": "main",
         "enqueued_at": "2026-01-02T00:00:00+00:00"},
    ]))


def _run(root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--control-root", str(root), *extra],
        capture_output=True, text=True)


def test_migration_converts_state_and_imp_can_read_it(tmp_path: Path) -> None:
    _seed_old_imp(tmp_path)
    r = _run(tmp_path)
    assert r.returncode == 0, r.stderr

    # 1. homes moved and change.json -> quest.json
    assert not (tmp_path / ".imp" / "changes").exists()
    assert (tmp_path / ".imp" / "quests" / "aaa111" / "quest.json").is_file()
    assert not (tmp_path / ".imp" / "quests" / "aaa111" / "change.json").exists()
    meta = json.loads(
        (tmp_path / ".imp" / "quests" / "bbb222" / "quest.json").read_text())
    assert meta["title"] == "Second Quest"

    # 2. records rekeyed change_meta -> quest_meta
    rec = json.loads(
        (tmp_path / ".imp" / "archive" / "records" / "aaa111.json").read_text())
    assert "quest_meta" in rec and "change_meta" not in rec

    # 3. merge queue rekeyed change_id -> quest_id
    mq = json.loads((tmp_path / ".git" / "imp" / "merge-queue.json").read_text())
    assert mq[0]["quest_id"] == "bbb222" and "change_id" not in mq[0]

    # 4. index rebuilt under the quests/quest_id schema; imp read paths work
    conn = index.connect(index_path(tmp_path))
    try:
        assert index.quest_ids(conn) == ["aaa111", "bbb222"]          # overview
        assert [row[0] for row in index.search(conn, "Second")] == ["bbb222"]  # search
        deps = conn.execute(
            "SELECT depends_on_id FROM deps WHERE quest_id = ?",
            ("bbb222",)).fetchall()
        assert deps == [("aaa111",)]                                  # deps resolve
    finally:
        conn.close()

    # imp log / resume read path: read_quest_json returns the migrated meta
    blob = read_quest_json(tmp_path, "aaa111")
    assert blob is not None and json.loads(blob)["title"] == "First Quest"


def test_migration_is_idempotent(tmp_path: Path) -> None:
    _seed_old_imp(tmp_path)
    assert _run(tmp_path).returncode == 0
    quest_json = (tmp_path / ".imp" / "quests" / "aaa111" / "quest.json").read_text()
    rec_before = (
        tmp_path / ".imp" / "archive" / "records" / "aaa111.json").read_text()

    second = _run(tmp_path)  # .imp/quests/ already exists -> clean no-op move
    assert second.returncode == 0, second.stderr
    assert not (tmp_path / ".imp" / "changes").exists()
    assert (tmp_path / ".imp" / "quests" / "aaa111"
            / "quest.json").read_text() == quest_json
    # record already has quest_meta, so the second pass leaves it byte-identical
    assert (tmp_path / ".imp" / "archive" / "records"
            / "aaa111.json").read_text() == rec_before
    conn = index.connect(index_path(tmp_path))
    try:
        assert index.quest_ids(conn) == ["aaa111", "bbb222"]
    finally:
        conn.close()


def test_dry_run_mutates_nothing(tmp_path: Path) -> None:
    _seed_old_imp(tmp_path)
    r = _run(tmp_path, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert (tmp_path / ".imp" / "changes" / "aaa111" / "change.json").is_file()
    assert not (tmp_path / ".imp" / "quests").exists()
    rec = json.loads(
        (tmp_path / ".imp" / "archive" / "records" / "aaa111.json").read_text())
    assert "change_meta" in rec
    mq = json.loads((tmp_path / ".git" / "imp" / "merge-queue.json").read_text())
    assert "change_id" in mq[0]


def test_errors_when_no_imp_dir(tmp_path: Path) -> None:
    r = _run(tmp_path)
    assert r.returncode == 2
    assert "no .imp/" in r.stderr

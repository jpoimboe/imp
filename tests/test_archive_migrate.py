from __future__ import annotations

import json
import subprocess
from pathlib import Path

from imp.engine import archive, worktree
from imp.engine.archive import index, migrate, record, records_root
from imp.engine.archive.blobstore import hash_content


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)


def _events_bytes(cid: str) -> bytes:
    return (
        f'{{"id":"{cid}-e0","parent_id":null,"agent_id":null,'
        '"timestamp":"2026-06-01T00:00:01+00:00","type":"change_created",'
        f'"payload":{{"id":"{cid}"}}}}\n'
        f'{{"id":"{cid}-e1","parent_id":"{cid}-e0","agent_id":"plan-writer",'
        '"timestamp":"2026-06-01T00:00:02+00:00","type":"step_started",'
        '"payload":{"step":"plan-writer"}}\n'
    ).encode()


def _journal_change(
    control: Path, cid: str, status: str, *, artifacts: dict[str, bytes] | None = None,
) -> None:
    """Stage one Change under refs/imp/journal:<cid>/ with runtime files and
    optional artifacts (path -> bytes, e.g. {"spec/x.md": b"..."})."""
    stage = control / "stage"
    stage.mkdir(parents=True, exist_ok=True)
    files: dict[str, Path] = {}
    (stage / "events.jsonl").write_bytes(_events_bytes(cid))
    files["events.jsonl"] = stage / "events.jsonl"
    (stage / "change.json").write_text(json.dumps({
        "id": cid, "title": cid.title(), "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": ["dep1"]}))
    files["change.json"] = stage / "change.json"
    (stage / "state.json").write_text(json.dumps({
        "id": cid, "status": status, "step": "finalize"}))
    files["state.json"] = stage / "state.json"
    for rel, data in (artifacts or {}).items():
        dest = stage / rel.replace("/", "__")
        dest.write_bytes(data)
        files[rel] = dest
    worktree.journal_commit(control, cid, files)


def test_import_change_runtime_builds_record_blob_and_index(tmp_path: Path) -> None:
    _git_init(tmp_path)
    _journal_change(tmp_path, "alpha", "completed")
    expected_hash = hash_content(_events_bytes("alpha"))

    assert migrate.import_change_runtime(tmp_path, "alpha") is True

    # Record file carries the matching content-addressed events_hash + the
    # status read out of the journal's state.json.
    rec = record.read_record(records_root(tmp_path), "alpha")
    assert rec is not None
    assert rec["events_hash"] == expected_hash
    assert rec["base"] == "main"
    assert rec["final_state"]["status"] == "completed"

    # Index has the change row + one event row per journal events.jsonl line.
    conn = index.connect(archive.index_path(tmp_path))
    try:
        assert conn.execute(
            "SELECT status FROM changes WHERE id='alpha'").fetchone() == ("completed",)
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE change_id='alpha'").fetchone()[0] == 2
    finally:
        conn.close()


def test_import_change_runtime_missing_change_returns_false(tmp_path: Path) -> None:
    _git_init(tmp_path)
    assert migrate.import_change_runtime(tmp_path, "ghost") is False

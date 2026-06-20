from __future__ import annotations

import json
import subprocess
from pathlib import Path

from imp.engine import archive, worktree
from imp.engine.archive import index, migrate, record, records_root
from imp.engine.archive.blobstore import hash_content
from imp.engine.archive.read import archived_ids, newest_plan_name, read_artifact


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


def _wip_ref_exists(control: Path, cid: str) -> bool:
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet",
         f"refs/imp/journal-wip/{cid}"],
        cwd=control, capture_output=True, text=True)
    return proc.returncode == 0


def test_import_change_artifacts_builds_wip_ref_from_journal(tmp_path: Path) -> None:
    _git_init(tmp_path)
    _journal_change(tmp_path, "alpha", "completed", artifacts={
        "spec/alpha.md": b"# alpha spec\n",
        "plans/alpha-plan-1.md": b"# alpha plan 1\n",
    })

    assert migrate.import_change_artifacts(tmp_path, "alpha") is True
    assert _wip_ref_exists(tmp_path, "alpha")
    # The artifacts are now readable off the retained per-Change ref, and the
    # runtime files are NOT carried into it.
    assert read_artifact(tmp_path, "alpha", "spec/alpha.md") == b"# alpha spec\n"
    assert newest_plan_name(tmp_path, "alpha") == "alpha-plan-1.md"


def test_import_change_artifacts_skips_when_ref_already_exists(tmp_path: Path) -> None:
    _git_init(tmp_path)
    _journal_change(tmp_path, "alpha", "completed", artifacts={
        "spec/alpha.md": b"# journal spec\n"})
    # A wip ref already carries this Change's spec (recent Changes have one).
    existing = tmp_path / "existing-spec.md"
    existing.write_bytes(b"# existing spec\n")
    worktree.artifact_commit(
        tmp_path, "alpha", {"spec/alpha.md": existing}, message="seed")

    assert migrate.import_change_artifacts(tmp_path, "alpha") is False
    # The pre-existing ref content is untouched (not overwritten from the journal).
    assert read_artifact(tmp_path, "alpha", "spec/alpha.md") == b"# existing spec\n"


def test_import_change_artifacts_no_artifacts_returns_false(tmp_path: Path) -> None:
    _git_init(tmp_path)
    _journal_change(tmp_path, "alpha", "completed")  # runtime files only
    assert migrate.import_change_artifacts(tmp_path, "alpha") is False
    assert not _wip_ref_exists(tmp_path, "alpha")


def test_import_journal_imports_every_change_and_is_idempotent(tmp_path: Path) -> None:
    _git_init(tmp_path)
    _journal_change(tmp_path, "alpha", "completed", artifacts={
        "spec/alpha.md": b"# alpha\n"})
    _journal_change(tmp_path, "beta", "abandoned")  # runtime only, no artifacts

    imported = migrate.import_journal(tmp_path)
    assert imported == ["alpha", "beta"]

    # Both Changes are now served from the index; the one with artifacts has a
    # per-Change ref, the runtime-only one does not.
    assert archived_ids(tmp_path) == ["alpha", "beta"]
    assert _wip_ref_exists(tmp_path, "alpha")
    assert not _wip_ref_exists(tmp_path, "beta")

    # Re-running does not duplicate index rows or error.
    again = migrate.import_journal(tmp_path)
    assert again == ["alpha", "beta"]
    conn = index.connect(archive.index_path(tmp_path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE change_id='alpha'").fetchone()[0] == 2
    finally:
        conn.close()


def test_import_journal_empty_ref_returns_empty(tmp_path: Path) -> None:
    _git_init(tmp_path)
    assert migrate.import_journal(tmp_path) == []


def _journal_ref_exists(control: Path) -> bool:
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "refs/imp/journal"],
        cwd=control, capture_output=True, text=True)
    return proc.returncode == 0


def test_retire_journal_deletes_ref_and_is_idempotent(tmp_path: Path) -> None:
    _git_init(tmp_path)
    _journal_change(tmp_path, "alpha", "completed")
    assert _journal_ref_exists(tmp_path)

    assert migrate.retire_journal(tmp_path) is True
    assert not _journal_ref_exists(tmp_path)

    # Already absent: a no-op that reports False rather than erroring.
    assert migrate.retire_journal(tmp_path) is False


def _journal_legacy_change(control: Path, cid: str) -> None:
    stage = control / "stage"
    stage.mkdir(parents=True, exist_ok=True)
    files: dict[str, Path] = {}
    (stage / "events.jsonl").write_bytes(_events_bytes(cid))
    files["events.jsonl"] = stage / "events.jsonl"
    (stage / "change.json").write_text(json.dumps({
        "slug": cid, "title": cid.title(), "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00"}))
    files["change.json"] = stage / "change.json"
    (stage / "state.json").write_text(json.dumps({
        "slug": cid, "status": "completed", "step": "finalize"}))
    files["state.json"] = stage / "state.json"
    worktree.journal_commit(control, cid, files)


def test_import_change_runtime_injects_id_for_legacy_slug_only(tmp_path: Path) -> None:
    _git_init(tmp_path)
    _journal_legacy_change(tmp_path, "2026-06-03-legacy")

    assert migrate.import_change_runtime(tmp_path, "2026-06-03-legacy") is True

    rec = record.read_record(records_root(tmp_path), "2026-06-03-legacy")
    assert rec is not None
    assert rec["id"] == "2026-06-03-legacy"
    assert rec["title"] == "2026-06-03-Legacy"
    assert rec["base"] == "main"


def test_import_change_runtime_preserves_present_id(tmp_path: Path) -> None:
    _git_init(tmp_path)
    # The journal ENTRY NAME ("entryname") differs from the embedded `id`
    # ("realid"), so a blind overwrite with change_id would change the record id.
    stage = tmp_path / "stage"
    stage.mkdir(parents=True, exist_ok=True)
    files: dict[str, Path] = {}
    (stage / "events.jsonl").write_bytes(_events_bytes("realid"))
    files["events.jsonl"] = stage / "events.jsonl"
    (stage / "change.json").write_text(json.dumps({
        "id": "realid", "title": "Real", "base_branch": "main"}))
    files["change.json"] = stage / "change.json"
    (stage / "state.json").write_text(json.dumps({"status": "completed"}))
    files["state.json"] = stage / "state.json"
    worktree.journal_commit(tmp_path, "entryname", files)

    assert migrate.import_change_runtime(tmp_path, "entryname") is True
    # setdefault leaves the present id intact: the record is keyed "realid",
    # NOT the entry name "entryname".
    rec = record.read_record(records_root(tmp_path), "realid")
    assert rec is not None
    assert rec["id"] == "realid"
    assert record.read_record(records_root(tmp_path), "entryname") is None

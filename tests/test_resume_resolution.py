from __future__ import annotations

import json
from pathlib import Path

import imp.cli.main as m
from imp.engine import archive


# _Row = (id, status, step, last_iso, title, created_at)
def _row(cid: str, status: str, last: str) -> tuple[str, str, str, str, str, str]:
    return (cid, status, "plan", last, "Title " + cid, "2026-06-01T00:00:00+00:00")


def test_resumable_rows_drops_running_and_sorts_newest_first(
    monkeypatch, tmp_path: Path,
) -> None:
    rows = [
        _row("a", "running", "2026-06-10T00:00:00+00:00"),       # excluded (executing)
        _row("b", "paused", "2026-06-11T00:00:00+00:00"),        # newest resumable
        _row("c", "new", "2026-06-09T00:00:00+00:00"),          # resumable at-rest
        _row("d", "merge_blocked", "2026-06-08T00:00:00+00:00"),
    ]
    monkeypatch.setattr(m, "_status_rows", lambda control: rows)
    monkeypatch.setattr(m, "_backlog_rows", lambda control: [])
    out = m._resumable_rows(tmp_path)
    assert [r[0] for r in out] == ["b", "c", "d"]                # running dropped, newest-first


def test_resumable_rows_includes_backlog_rows(monkeypatch, tmp_path: Path) -> None:
    backlog_row = _row("s1", "idea", "2026-06-12T00:00:00+00:00")
    monkeypatch.setattr(m, "_status_rows", lambda control: [])
    monkeypatch.setattr(m, "_backlog_rows", lambda control: [backlog_row])
    out = m._resumable_rows(tmp_path)
    assert [r[0] for r in out] == ["s1"]


def test_past_change_count_counts_completed_rows(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        m, "_completed_rows",
        lambda control: [_row("x", "completed", ""), _row("y", "abandoned", "")])
    assert m._past_change_count(tmp_path) == 2


def _git_init(root: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)


def _archive_with_last_ts(control: Path, cid: str, last_ts: str) -> None:
    events = control / f"{cid}.jsonl"
    events.write_bytes((
        f'{{"id":"{cid}-e0","timestamp":"2026-06-01T00:00:00+00:00",'
        f'"type":"change_created","payload":{{"id":"{cid}"}}}}\n'
        f'{{"id":"{cid}-e1","timestamp":"{last_ts}",'
        '"type":"step_started","payload":{"step":"plan-writer"}}\n'
    ).encode())
    cj = control / f"{cid}-quest.json"
    cj.write_text(json.dumps({
        "id": cid, "title": cid.title(), "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    sj = control / f"{cid}-state.json"
    sj.write_text(json.dumps({"id": cid, "status": "completed", "step": None}))
    archive.archive_quest(
        control, cid, events_path=events,
        quest_json_path=cj, state_json_path=sj)


def test_resolve_default_id_uses_index_not_blob(monkeypatch, tmp_path: Path) -> None:
    from imp.engine.archive.blobstore import LocalBlobStore

    _git_init(tmp_path)
    _archive_with_last_ts(tmp_path, "older", "2026-06-01T00:00:05+00:00")
    _archive_with_last_ts(tmp_path, "newer", "2026-06-09T00:00:05+00:00")

    def _boom_get(self, digest):
        raise AssertionError("_resolve_default_id must not read an event blob")

    monkeypatch.setattr(LocalBlobStore, "get", _boom_get)
    assert m._resolve_default_id(tmp_path) == "newer"

from __future__ import annotations

import json
from pathlib import Path

import imp.cli.main as m
from imp.engine import archive


def test_resume_gone_abandoned(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(m, "_journal_status", lambda c, i: "abandoned")
    monkeypatch.setattr(m, "_title_for_id", lambda c, i: "Add version flag")
    rc = m._resume_gone(tmp_path, "2026-06-11-yaitd7")
    out, err = capsys.readouterr()
    assert rc == 0
    assert "Add version flag was abandoned; nothing to resume." in out
    assert err == ""                              # no leaked path / internal error


def test_resume_gone_completed(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(m, "_journal_status", lambda c, i: "completed")
    monkeypatch.setattr(m, "_title_for_id", lambda c, i: "Add version flag")
    rc = m._resume_gone(tmp_path, "2026-06-11-yaitd7")
    out, err = capsys.readouterr()
    assert rc == 0
    assert "Add version flag is already completed; nothing to resume." in out
    assert err == ""


def test_resume_gone_unknown_keeps_no_match(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(m, "_journal_status", lambda c, i: None)
    monkeypatch.setattr(m, "_title_for_id", lambda c, i: None)
    rc = m._resume_gone(tmp_path, "2026-06-11-yaitd7")
    out, err = capsys.readouterr()
    assert rc == 2
    assert "imp: no Change matches '2026-06-11-yaitd7'" in err
    assert out == ""


def test_journal_status_reads_status_field(tmp_path: Path) -> None:
    events = tmp_path / "e.jsonl"
    events.write_text("")
    cj = tmp_path / "c.json"
    cj.write_text(json.dumps({
        "id": "2026-06-01-xyz999", "title": "X", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    sj = tmp_path / "s.json"
    sj.write_text(json.dumps({
        "id": "2026-06-01-xyz999", "status": "completed", "step": "merge"}))
    archive.archive_quest(
        tmp_path, "2026-06-01-xyz999", events_path=events,
        quest_json_path=cj, state_json_path=sj)
    assert m._journal_status(tmp_path, "2026-06-01-xyz999") == "completed"


def test_journal_status_absent_returns_none(tmp_path: Path) -> None:
    assert m._journal_status(tmp_path, "x") is None

from __future__ import annotations

from pathlib import Path

import imp.cli.main as m


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


def test_journal_status_reads_status_field(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        m.journal, "journal_show",
        lambda control, cid, name: b'{"status":"completed","step":"merge"}'
        if name == "state.json" else None)
    assert m._journal_status(tmp_path, "2026-06-01-xyz999") == "completed"


def test_journal_status_absent_returns_none(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(m.journal, "journal_show", lambda control, cid, name: None)
    assert m._journal_status(tmp_path, "x") is None

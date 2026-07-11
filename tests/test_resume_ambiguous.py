from __future__ import annotations

import json
from pathlib import Path

import pytest

import imp.cli.main as m
from imp.engine import archive


def test_row_for_id_from_archive(tmp_path: Path) -> None:
    events = tmp_path / "e.jsonl"
    events.write_text(
        '{"timestamp":"2026-06-05T00:00:00+00:00","type":"change_created",'
        '"payload":{}}\n')
    cj = tmp_path / "c.json"
    cj.write_text(json.dumps({
        "id": "2026-06-01-xyz999", "title": "Done", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    sj = tmp_path / "s.json"
    sj.write_text(json.dumps({
        "id": "2026-06-01-xyz999", "status": "completed", "step": "merge"}))
    archive.archive_quest(
        tmp_path, "2026-06-01-xyz999", events_path=events,
        quest_json_path=cj, state_json_path=sj)
    row = m._row_for_id(tmp_path, "2026-06-01-xyz999")
    assert row == ("2026-06-01-xyz999", "completed", "merge",
                   "2026-06-05T00:00:00+00:00", "Done", "2026-06-01T00:00:00+00:00",
                   None, None)


def test_exit_ambiguous_renders_each_candidate_via_formatter(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    def fake_row(control, cid):
        # _Row = (id, status, step, last_iso, title, created_at, step_label, phase_label)
        return (cid, "paused", "plan", "2026-06-11T11:45:00+00:00",
                "Title " + cid[-3:], "2026-06-11T00:00:00+00:00", None, None)

    monkeypatch.setattr(m, "_row_for_id", fake_row)
    with pytest.raises(SystemExit) as exc:
        m._exit_ambiguous(
            "2026-06-04", ["2026-06-04-aaa111", "2026-06-04-bbb222"], tmp_path)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "is ambiguous" in err
    assert "NAME" in err                          # shared header row
    assert "paused" in err                        # STATUS via the formatter
    assert "aaa111" in err and "bbb222" in err    # resume tokens, one row each

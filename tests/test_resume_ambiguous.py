from __future__ import annotations

import json
from pathlib import Path

import pytest

import imp.cli.main as m
import imp.cli.store as s
from imp.engine import archive


def test_row_for_id_from_archive(tmp_path: Path) -> None:
    events = tmp_path / "e.jsonl"
    events.write_text(
        '{"timestamp":"2026-06-05T00:00:00+00:00","type":"quest_created",'
        '"payload":{}}\n')
    cj = tmp_path / "c.json"
    cj.write_text(json.dumps({
        "id": "2026-06-01-xyz999", "title": "Done", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    sj = tmp_path / "s.json"
    sj.write_text(json.dumps({
        "id": "2026-06-01-xyz999", "status": "merged", "step": "merge"}))
    archive.archive_quest(
        tmp_path, "2026-06-01-xyz999", events_path=events,
        quest_json_path=cj, state_json_path=sj)
    row = s._row_for_id(tmp_path, "2026-06-01-xyz999")
    assert row == ("2026-06-01-xyz999", "merged", "merge",
                   "2026-06-05T00:00:00+00:00", "Done", "2026-06-01T00:00:00+00:00",
                   None, None, None)


def test_exit_ambiguous_renders_each_candidate_via_quest_card(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    from imp.cli.store import QuestCard

    def fake_card(cid: str) -> QuestCard:
        return QuestCard(
            quest_id=cid, status="paused", pause_reason=None, step="plan",
            last="2026-06-11T11:45:00+00:00", title="Title " + cid[-3:],
            created="2026-06-11T00:00:00+00:00", step_label=None, phase_label=None,
            workflow="default", base_branch=None, merge_commit=None, parent=None,
            depends_on=[], intent=None, seed_opening=None, summary=None, vars={})

    class FakeStore:
        def quest_card(self, cid: str) -> QuestCard:
            return fake_card(cid)

    monkeypatch.setattr("imp.cli.store.make_store", lambda control: FakeStore())
    with pytest.raises(SystemExit) as exc:
        m._exit_ambiguous(
            "2026-06-04", ["2026-06-04-aaa111", "2026-06-04-bbb222"], tmp_path)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "imp: " in err
    assert "is ambiguous" in err
    assert "NAME" in err                          # shared header row
    assert "paused" in err                        # STATUS via card.status
    assert "aaa111" in err and "bbb222" in err    # resume tokens, one row each

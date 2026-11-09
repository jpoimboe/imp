from __future__ import annotations

import json
from pathlib import Path

import pytest

import imp.cli.main as m
import imp.cli.store as s
from imp.engine import archive
from imp.engine.quest import Quest
from tests.conftest import _seed_control


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
        "id": "2026-06-01-xyz999", "status": "completed", "step": "merge"}))
    archive.archive_quest(
        tmp_path, "2026-06-01-xyz999", events_path=events,
        quest_json_path=cj, state_json_path=sj)
    row = s._row_for_id(tmp_path, "2026-06-01-xyz999")
    assert row == ("2026-06-01-xyz999", "completed", "merge",
                   "2026-06-05T00:00:00+00:00", "Done", "2026-06-01T00:00:00+00:00",
                   None, None, None)


def test_exit_ambiguous_renders_each_candidate_via_quest_card(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    from imp.cli.store import QuestCard

    def fake_card(cid: str) -> QuestCard:
        return QuestCard(
            quest_id=cid, status="paused", pause_reason=None, question=None, step="plan",
            last="2026-06-11T11:45:00+00:00", title="Title " + cid[-3:],
            created="2026-06-11T00:00:00+00:00", step_label=None, phase_label=None,
            workflow="default", base_branch=None, merge_commit=None, parent=None,
            depends_on=[], intent=None, seed_opening=None, summary=None, files=[], vars={},
            seeded_vars={})

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
    assert "TITLE" in err                          # shared header row
    assert "paused" in err                        # STATUS via card.status
    assert "aaa111" in err and "bbb222" in err    # resume tokens, one row each


def test_exit_ambiguous_names_titleless_candidates_by_their_opening(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    _seed_control(tmp_path)
    Quest.create_draft(tmp_path, title="", quest_id="amb001", base_branch="main",
                       seed_opening="summarize\n  foo.py")
    q2 = Quest.create(tmp_path, title="", quest_id="amb002", base_branch="main")
    q2.event_log().append(
        agent_id=None, type="user_message",
        payload={"subtype": "intent",
                 "content": [{"type": "text", "text": "fix the login crash"}]})
    monkeypatch.setattr(m, "should_use_color", lambda stream: False)
    with pytest.raises(SystemExit):
        m._exit_ambiguous("amb", ["amb001", "amb002"], tmp_path)
    err = capsys.readouterr().err
    lines = err.splitlines()
    assert "summarize foo.py" in next(ln for ln in lines if ln.startswith("amb001"))
    assert "fix the login crash" in next(ln for ln in lines if ln.startswith("amb002"))
    assert "(untitled)" not in err


def test_exit_ambiguous_sizes_columns_to_content(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    import os
    import re

    from imp.cli.store import QuestCard

    class FakeStore:
        def quest_card(self, cid: str) -> QuestCard:
            return QuestCard(
                quest_id=cid, status="paused", pause_reason=None, question=None, step="plan",
                last="2026-06-11T11:45:00+00:00", title="Title " + cid[-3:],
                created="2026-06-11T00:00:00+00:00", step_label=None, phase_label=None,
                workflow="default", base_branch=None, merge_commit=None, parent=None,
                depends_on=[], intent=None, seed_opening=None, summary=None, files=[], vars={},
                seeded_vars={})

    monkeypatch.setattr("imp.cli.store.make_store", lambda control: FakeStore())
    monkeypatch.setattr(m, "should_use_color", lambda stream: False)
    monkeypatch.setattr("shutil.get_terminal_size",
                        lambda fallback=(80, 24): os.terminal_size((80, 24)))
    with pytest.raises(SystemExit):
        m._exit_ambiguous("a", ["aaa111", "abb222"], tmp_path)
    lines = capsys.readouterr().err.splitlines()
    assert lines[1] == "ID      TITLE       STATUS         UPDATED"
    assert re.fullmatch(r"aaa111  Title 111   paused \(plan\)  \d+[smhd]", lines[2])
    assert re.fullmatch(r"abb222  Title 222   paused \(plan\)  \d+[smhd]", lines[3])


def test_exit_ambiguous_sizes_around_missing_and_titleless_cards(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    import os
    import re

    from imp.cli.store import QuestCard

    def card(cid: str, title: str | None, seed_opening: str | None) -> QuestCard:
        return QuestCard(
            quest_id=cid, status="paused", pause_reason=None, question=None, step="plan",
            last="2026-06-11T11:45:00+00:00", title=title,
            created="2026-06-11T00:00:00+00:00", step_label=None, phase_label=None,
            workflow="default", base_branch=None, merge_commit=None, parent=None,
            depends_on=[], intent=None, seed_opening=seed_opening, summary=None, files=[],
            vars={}, seeded_vars={})

    class FakeStore:
        def quest_card(self, cid: str) -> QuestCard | None:
            return {
                "aaa111": card(cid, "Title 111", None),
                "abb222": card(cid, None, "Fix the flaky relay test"),
                "acc333": None,
            }[cid]

    monkeypatch.setattr("imp.cli.store.make_store", lambda control: FakeStore())
    monkeypatch.setattr(m, "should_use_color", lambda stream: False)
    monkeypatch.setattr("shutil.get_terminal_size",
                        lambda fallback=(80, 24): os.terminal_size((80, 24)))
    with pytest.raises(SystemExit):
        m._exit_ambiguous("a", ["aaa111", "abb222", "acc333"], tmp_path)
    lines = capsys.readouterr().err.splitlines()
    assert lines[1] == "ID      TITLE                     STATUS         UPDATED"
    assert re.fullmatch(
        r"aaa111  Title 111                 paused \(plan\)  \d+[smhd]", lines[2])
    assert re.fullmatch(
        r"abb222  Fix the flaky relay test  paused \(plan\)  \d+[smhd]", lines[3])
    assert lines[4] == "  acc333"

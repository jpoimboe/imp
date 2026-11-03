from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import imp.cli.main as m
from imp.cli import palette as pal
from imp.create import create_draft_quest
from imp.engine.quest import Quest
from imp.paths import project_state_dir
from tests.conftest import _seed_control
from tests.test_journal_read_cli import _complete_and_archive

_NOW = datetime(2026, 6, 28, tzinfo=UTC)


def _archive_unnamed(control: Path, cid: str, *, prompt: str | None) -> None:
    """Archive a title-less completed quest, optionally with an opening prompt."""
    ch = Quest.create(control, title="", quest_id=cid, base_branch="main")
    if prompt is not None:
        with ch.events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "id": "open", "agent_id": None,
                "timestamp": "2026-06-01T00:00:00+00:00",
                "type": "user_message",
                "payload": {"role": "user", "subtype": "intent",
                            "content": [{"type": "text", "text": prompt}]}}) + "\n")
    _complete_and_archive(control, cid)


def test_list_all_unnamed_archived_shows_opening_prompt(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_unnamed(tmp_path, "unn001", prompt="add retry budget")
    lines = m._overview_lines(tmp_path, show_all=True, now=_NOW, color=False)
    row = next(ln for ln in lines if ln.startswith("unn001"))
    assert "add retry budget" in row
    assert "(unnamed)" not in row


def test_list_all_promptless_archived_still_unnamed(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_unnamed(tmp_path, "unn002", prompt=None)
    lines = m._overview_lines(tmp_path, show_all=True, now=_NOW, color=False)
    row = next(ln for ln in lines if ln.startswith("unn002"))
    assert "(unnamed)" in row


def test_list_all_titled_archived_shows_title(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="refactor login", quest_id="tit001", base_branch="main")
    _complete_and_archive(tmp_path, "tit001")
    lines = m._overview_lines(tmp_path, show_all=True, now=_NOW, color=False)
    row = next(ln for ln in lines if ln.startswith("tit001"))
    assert "refactor login" in row


def _overview_row(control: Path, cid: str, *, color: bool = False) -> str:
    lines = m._overview_lines(control, show_all=True, now=_NOW, color=color)
    return next(ln for ln in lines if ln.startswith(cid))


def test_list_names_an_unstarted_draft_by_its_prompt(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    q = create_draft_quest(tmp_path, "summarize\n  foo.py")
    row = _overview_row(tmp_path, q.id)
    assert "summarize foo.py" in row
    assert "(unnamed)" not in row


def test_list_dims_the_seed_fallback_name(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    q = create_draft_quest(tmp_path, "summarize\n  foo.py")
    assert pal.ansi("summarize foo.py", "dim") in _overview_row(tmp_path, q.id, color=True)


def test_list_names_a_titleless_backlog_quest_by_its_brief(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Quest.create_draft(tmp_path, title="", quest_id="bk0001", base_branch="main",
                       origin="idea", seed_opening="Brief one\n\nmore body")
    row = _overview_row(tmp_path, "bk0001")
    assert "Brief one more body" in row
    assert "(unnamed)" not in row


def test_list_names_a_homeless_archived_quest_by_its_seed_opening(tmp_path: Path) -> None:
    # No live home: the name must come from the archived quest.json's seed_opening.
    _seed_control(tmp_path)
    q = Quest.create(tmp_path, title="", quest_id="arc002", base_branch="main")
    q.set_seed_opening("seeded only")
    _complete_and_archive(tmp_path, "arc002")
    shutil.rmtree(project_state_dir(tmp_path) / "quests" / "arc002")
    row = _overview_row(tmp_path, "arc002")
    assert "seeded only" in row
    assert "(unnamed)" not in row


def test_list_json_keeps_events_only_intent_for_seeded_quests(tmp_path: Path) -> None:
    # `imp list --json` must not pick up the human-only seed fallback.
    _seed_control(tmp_path)
    q = create_draft_quest(tmp_path, "summarize foo.py")
    Quest.create_draft(tmp_path, title="", quest_id="bk0001", base_branch="main",
                       origin="idea", seed_opening="a brief")
    entries = {e["id"]: e for e in m._board_json(
        tmp_path, dispositions=m._board_dispositions(show_all=True), cutoff=None)}
    assert entries[q.id]["title"] is None and entries[q.id]["intent"] is None
    assert entries["bk0001"]["title"] is None and entries["bk0001"]["intent"] is None

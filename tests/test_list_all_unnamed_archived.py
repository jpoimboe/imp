from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import imp.cli.main as m
from imp.engine.quest import Quest
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
                "type": "opening_intent",
                "payload": {"text": prompt}}) + "\n")
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

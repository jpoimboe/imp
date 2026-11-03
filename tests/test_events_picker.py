from __future__ import annotations

from pathlib import Path

import imp.cli.main as m
import imp.cli.store as s
from imp.cli.main import main

_EVENTS_NO_QUESTS = "imp: events needs a quest id\n  No quests yet — start one with imp new\n"


def test_bare_events_in_empty_project_says_start_one(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    # Bare `imp events` names the missing id on stderr and exits 2.
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    rc = main(["events"])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.err == _EVENTS_NO_QUESTS
    assert captured.out == ""


def test_bare_events_message_identical_when_interactive(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_interactive", lambda: True)
    rc = main(["events"])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.err == _EVENTS_NO_QUESTS
    assert captured.out == ""


def test_events_ambiguous_token_lists_candidates_exit_2(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    # AC 4: an ambiguous token routes through _resolve_id -> _exit_ambiguous;
    # no picker, even when interactive.
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_interactive", lambda: True)
    monkeypatch.setattr(
        m, "_live_quest_ids", lambda c: ["2026-06-10-foo", "2026-06-11-foo"])
    monkeypatch.setattr(s, "_row_for_id", lambda c, cid: None)
    rc = main(["events", "foo"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "is ambiguous; candidates:" in err


def test_events_unknown_token_errors_exit_2(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_live_quest_ids", lambda c: ["2026-06-10-foo"])
    rc = main(["events", "zzz"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "no quest matches" in err

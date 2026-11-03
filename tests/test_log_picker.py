from __future__ import annotations

from pathlib import Path

import imp.cli.main as m
import imp.cli.store as s
from imp.cli.main import main
from tests.conftest import _seed_control
from tests.test_inflight_sort import _seed_inflight

_LOG_NO_QUESTS = "imp: log needs a quest id\n  No quests yet — start one with imp new\n"


def test_bare_log_in_empty_project_says_start_one(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    # Bare `imp log` names the missing id on stderr and exits 2 —
    # no picker, no most-recent default.
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    rc = main(["log"])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.err == _LOG_NO_QUESTS
    assert captured.out == ""


def test_bare_log_message_identical_when_interactive(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_interactive", lambda: True)
    rc = main(["log"])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.err == _LOG_NO_QUESTS
    assert captured.out == ""


def test_bare_log_with_quests_present_points_at_list(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    # Even with a resolvable quest, bare `imp log` never targets the most-recent one.
    _seed_control(tmp_path)
    _seed_inflight(tmp_path, "live01", created="2026-06-01T00:00:00+00:00",
                   started="2026-06-01T00:00:00+00:00")
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    rc = main(["log"])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.err == "imp: log needs a quest id — see imp list\n"
    assert captured.out == ""


def test_log_ambiguous_token_lists_candidates_exit_2(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    # An ambiguous token resolves through _resolve_id -> _exit_ambiguous.
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_interactive", lambda: True)
    monkeypatch.setattr(
        m, "_live_quest_ids", lambda c: ["2026-06-10-foo", "2026-06-11-foo"])
    monkeypatch.setattr(s, "_row_for_id", lambda c, cid: None)
    rc = main(["log", "foo"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "is ambiguous; candidates:" in err


def test_log_unknown_token_errors_exit_2(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_live_quest_ids", lambda c: ["2026-06-10-foo"])
    rc = main(["log", "zzz"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "no quest matches" in err

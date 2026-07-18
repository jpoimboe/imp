from __future__ import annotations

from pathlib import Path

import imp.cli.main as m
from imp.cli.main import main


def test_bare_log_prints_help_to_stderr_exit_2(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    # AC 2: bare `imp log` prints its own help to stderr and exits 2 —
    # no picker, no most-recent default. The no-id check runs after the
    # --follow conflict guard, so _control_root must resolve.
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    rc = main(["log"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "usage: imp log" in captured.err
    assert "--full" in captured.err
    assert captured.out == ""


def test_bare_log_help_identical_when_interactive(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_interactive", lambda: True)
    rc = main(["log"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "usage: imp log" in captured.err
    assert captured.out == ""


def test_bare_log_with_quests_present_still_prints_help(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    # AC 8: even with resolvable quests, bare `imp log` prints help and exits 2 —
    # it never silently targets the most-recent quest.
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(
        m, "_iter_quest_ids", lambda c: ["2026-06-10-aaa", "2026-06-12-bbb"])
    rc = main(["log"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "usage: imp log" in captured.err


def test_log_ambiguous_token_lists_candidates_exit_2(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    # An ambiguous token resolves through _resolve_id -> _exit_ambiguous.
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_interactive", lambda: True)
    monkeypatch.setattr(
        m, "_iter_quest_ids", lambda c: ["2026-06-10-foo", "2026-06-11-foo"])
    monkeypatch.setattr(m, "_row_for_id", lambda c, cid: None)
    rc = main(["log", "foo"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "is ambiguous; candidates:" in err


def test_log_unknown_token_errors_exit_2(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_iter_quest_ids", lambda c: ["2026-06-10-foo"])
    rc = main(["log", "zzz"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "no quest matches" in err

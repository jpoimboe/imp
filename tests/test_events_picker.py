from __future__ import annotations

from pathlib import Path

import imp.cli.main as m
from imp.cli.main import main


def test_bare_events_prints_help_to_stderr_exit_2(capsys) -> None:
    # AC 3: bare `imp events` prints its own help to stderr and exits 2.
    rc = main(["events"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "usage: imp events" in captured.err
    assert "--type" in captured.err
    assert captured.out == ""


def test_bare_events_help_identical_when_interactive(monkeypatch, capsys) -> None:
    monkeypatch.setattr(m, "_interactive", lambda: True)
    rc = main(["events"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "usage: imp events" in captured.err
    assert captured.out == ""


def test_events_ambiguous_token_lists_candidates_exit_2(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    # AC 4: an ambiguous token routes through _resolve_id -> _exit_ambiguous;
    # no picker, even when interactive.
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_interactive", lambda: True)
    monkeypatch.setattr(
        m, "_iter_quest_ids", lambda c: ["2026-06-10-foo", "2026-06-11-foo"])
    monkeypatch.setattr(m, "_row_for_id", lambda c, cid: None)
    rc = main(["events", "foo"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "is ambiguous; candidates:" in err


def test_events_unknown_token_errors_exit_2(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_iter_quest_ids", lambda c: ["2026-06-10-foo"])
    rc = main(["events", "zzz"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "no quest matches" in err

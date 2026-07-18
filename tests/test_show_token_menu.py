from __future__ import annotations

import argparse
from pathlib import Path

import imp.cli.main as m
from imp.cli.main import main


def _ns(token, resource=None):
    return argparse.Namespace(token=token, resource=resource, no_pager=True, json=False)


def test_bare_show_prints_help_to_stderr_exit_2(capsys) -> None:
    # AC 1: bare `imp show` prints its own help to stderr and exits 2, no picker.
    rc = main(["show"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "usage: imp show" in captured.err
    assert "--json" in captured.err          # the command's full help, not a one-liner
    assert captured.out == ""


def test_bare_show_help_identical_when_interactive(monkeypatch, capsys) -> None:
    # AC 1: no interactive branch — even with _interactive() True, help prints, exit 2.
    monkeypatch.setattr(m, "_interactive", lambda: True)
    rc = main(["show"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "usage: imp show" in captured.err
    assert captured.out == ""


def test_show_ambiguous_token_lists_candidates_exit_2(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    # AC 4: an ambiguous token routes through _resolve_id -> _exit_ambiguous
    # (candidate list on stderr, exit 2); no picker, even when interactive.
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_interactive", lambda: True)
    monkeypatch.setattr(
        m, "_iter_quest_ids", lambda c: ["2026-06-10-foo", "2026-06-11-foo"])
    monkeypatch.setattr(m, "_row_for_id", lambda c, cid: None)
    rc = m._cmd_show(_ns("foo"))
    err = capsys.readouterr().err
    assert rc == 2
    assert "is ambiguous; candidates:" in err


def test_show_single_match_dispatches(monkeypatch, tmp_path: Path) -> None:
    # A token matching exactly one id resolves directly and dispatches, no picker.
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_resolve_id", lambda c, t: "2026-06-10-uniq")
    monkeypatch.setattr(m, "_unstarted_backlog_meta", lambda c, cid: None)
    seen: dict[str, str] = {}

    def fake_detail(control, quest_id, *, now, color):
        seen["id"] = quest_id
        return []

    monkeypatch.setattr(m, "_detail_card", fake_detail)
    monkeypatch.setattr(
        m, "_page_or_print", lambda lines, *, no_pager, trailing_blank=False: None)
    rc = m._cmd_show(_ns("uniq"))
    assert rc == 0
    assert seen["id"] == "2026-06-10-uniq"


def test_show_empty_token_errors_no_match(monkeypatch, tmp_path: Path, capsys) -> None:
    # An empty (non-None) token is not the no-id case: it flows to _resolve_id,
    # whose empty-token guard prints "no quest matches" and exits 2.
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_iter_quest_ids", lambda c: ["2026-06-10-foo"])
    rc = m._cmd_show(_ns(""))
    err = capsys.readouterr().err
    assert rc == 2
    assert "no quest matches" in err

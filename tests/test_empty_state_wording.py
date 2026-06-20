from __future__ import annotations

import argparse
from pathlib import Path

import imp.cli.main as m


def _empty_control(tmp_path: Path, monkeypatch) -> Path:
    # No worktrees and no journal → no Changes anywhere.
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_list_change_homes", lambda c: [])
    monkeypatch.setattr(m, "_resolve_default_id", lambda c: None)
    return tmp_path


def test_status_and_log_share_one_empty_message(tmp_path, monkeypatch, capsys) -> None:
    _empty_control(tmp_path, monkeypatch)

    rc_status = m._cmd_status(argparse.Namespace(all=False))
    status_out = capsys.readouterr()
    assert rc_status == 0
    assert m._NO_CHANGES_MESSAGE in status_out.out

    rc_log = m._cmd_log(argparse.Namespace(token=None, full=False))
    log_out = capsys.readouterr()
    assert rc_log == 0
    assert m._NO_CHANGES_MESSAGE in log_out.out
    assert "imp: no Changes found" not in log_out.err   # no error-prefixed register
    assert log_out.err == ""                             # empty case is non-error

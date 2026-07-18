from __future__ import annotations

import argparse
from pathlib import Path

import imp.cli.main as m


def test_status_empty_message(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_list_quest_homes", lambda c: [])
    rc_status = m._cmd_status(argparse.Namespace(all=False))
    status_out = capsys.readouterr()
    assert rc_status == 0
    assert m._NO_QUESTS_MESSAGE in status_out.out

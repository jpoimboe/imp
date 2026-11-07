from __future__ import annotations

from pathlib import Path

import imp.cli.main as m
import imp.cli.store as s


def test_list_empty_message(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(s, "_list_quest_homes", lambda c: [])
    assert m.main(["list"]) == 0
    assert m._NO_QUESTS_MESSAGE in capsys.readouterr().out

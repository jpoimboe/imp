from __future__ import annotations

import argparse
import io
import os
from contextlib import redirect_stdout

import imp.cli.main as m
from imp.cli.main import _cmd_status, main
from imp.engine.quest import Quest
from tests.conftest import _seed_control


def _term(monkeypatch, cols):
    monkeypatch.setattr("shutil.get_terminal_size",
                        lambda fallback=(80, 24): os.terminal_size((cols, 24)))


def test_list_caps_name_column_on_wide_terminal(tmp_path, monkeypatch):
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="L" * 70, quest_id="alpha", base_branch="main")
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")
    _term(monkeypatch, 200)
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert main(["list"]) == 0
    out = buf.getvalue()
    assert "L" * 59 + "…" in out       # NAME capped at 60 -> 59 chars + ellipsis
    assert "L" * 70 not in out          # the full 70-char title is never printed


def test_status_keeps_full_name_column_on_wide_terminal(tmp_path, monkeypatch, capsys):
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="L" * 70, quest_id="alpha", base_branch="main")
    monkeypatch.setenv("NO_COLOR", "1")
    _term(monkeypatch, 200)
    assert _cmd_status(argparse.Namespace(all=False), tmp_path) == 0
    out = capsys.readouterr().out
    assert "L" * 70 in out              # status is uncapped: full title shown


def test_filtered_list_caps_name_column_on_wide_terminal(tmp_path, monkeypatch):
    # The disposition-filtered path (`--active`) routes through
    # _filtered_overview_lines and must honor the cap too.
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="L" * 70, quest_id="alpha", base_branch="main")
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")
    _term(monkeypatch, 200)
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert main(["list", "--active"]) == 0
    out = buf.getvalue()
    assert "L" * 59 + "…" in out
    assert "L" * 70 not in out

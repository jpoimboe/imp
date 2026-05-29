# tests/test_cli_view.py
from __future__ import annotations

import io
import json

from imp.cli.main import _use_tui, main


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def test_use_tui_false_when_not_a_tty() -> None:
    assert _use_tui(no_tui=False, stream=_Pipe()) is False


def test_use_tui_false_when_no_tui_flag_even_on_tty() -> None:
    assert _use_tui(no_tui=True, stream=_TTY()) is False


def test_use_tui_true_on_tty_without_flag() -> None:
    assert _use_tui(no_tui=False, stream=_TTY()) is True


def test_log_no_tui_renders_narrative_text(tmp_path, monkeypatch, capsys) -> None:
    cdir = tmp_path / ".imp" / "changes" / "1"
    cdir.mkdir(parents=True)
    (cdir / "change.json").write_text(json.dumps({"id": 1, "title": "demo"}))
    (cdir / "events.jsonl").write_text(
        json.dumps({"id": "e1", "agent_id": None, "type": "change_created",
                    "payload": {"change_id": 1, "title": "demo"}}) + "\n"
        + json.dumps({"id": "e2", "agent_id": None, "type": "phase_started",
                      "payload": {"phase": "design"}}) + "\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)
    rc = main(["log", "1", "--no-tui"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "#1" in out and "demo" in out and "design" in out
    assert "{" not in out          # narrative, not raw json


# add to tests/test_cli_view.py
def test_log_on_tty_degrades_to_text_before_tui_exists(tmp_path, monkeypatch):
    import imp.cli.main as m
    cdir = tmp_path / ".imp" / "changes" / "1"
    cdir.mkdir(parents=True)
    (cdir / "change.json").write_text('{"id": 1, "title": "demo"}')
    (cdir / "events.jsonl").write_text(
        '{"id":"e1","agent_id":null,"type":"change_created",'
        '"payload":{"change_id":1,"title":"demo"}}\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_use_tui", lambda **k: True)        # pretend TTY
    monkeypatch.setattr(m, "_tui_available", lambda: False)     # tui.py absent
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = m.main(["log", "1"])
    assert rc == 0 and "demo" in buf.getvalue()                # text fallback, no ImportError

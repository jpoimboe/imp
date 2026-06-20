from __future__ import annotations

from pathlib import Path

import pytest

import imp.cli.main as m


def test_row_for_id_from_journal(monkeypatch, tmp_path: Path) -> None:
    def fake_show(control, cid, name):
        if name == "state.json":
            return b'{"status":"completed","step":"merge"}'
        if name == "change.json":
            return b'{"title":"Done","created_at":"2026-06-01T00:00:00+00:00"}'
        return None

    monkeypatch.setattr(m.journal, "journal_show", fake_show)
    monkeypatch.setattr(m, "_journal_last_iso", lambda c, i: "2026-06-05T00:00:00+00:00")
    row = m._row_for_id(tmp_path, "2026-06-01-xyz999")
    assert row == ("2026-06-01-xyz999", "completed", "merge",
                   "2026-06-05T00:00:00+00:00", "Done", "2026-06-01T00:00:00+00:00")


def test_exit_ambiguous_renders_each_candidate_via_formatter(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    def fake_row(control, cid):
        # _Row = (id, status, step, last_iso, title, created_at)
        return (cid, "paused", "plan", "2026-06-11T11:45:00+00:00",
                "Title " + cid[-3:], "2026-06-11T00:00:00+00:00")

    monkeypatch.setattr(m, "_row_for_id", fake_row)
    with pytest.raises(SystemExit) as exc:
        m._exit_ambiguous(
            "2026-06-04", ["2026-06-04-aaa111", "2026-06-04-bbb222"], tmp_path)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "is ambiguous" in err
    assert "NAME" in err                          # shared header row
    assert "paused" in err                        # STATUS via the formatter
    assert "aaa111" in err and "bbb222" in err    # resume tokens, one row each

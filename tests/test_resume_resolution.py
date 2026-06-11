from __future__ import annotations

from pathlib import Path

import imp.cli.main as m


# _Row = (id, status, step, last_iso, title, created_at)
def _row(cid: str, status: str, last: str) -> tuple[str, str, str, str, str, str]:
    return (cid, status, "plan", last, "Title " + cid, "2026-06-01T00:00:00+00:00")


def test_resumable_rows_drops_running_and_sorts_newest_first(
    monkeypatch, tmp_path: Path,
) -> None:
    rows = [
        _row("a", "running", "2026-06-10T00:00:00+00:00"),       # excluded (executing)
        _row("b", "paused", "2026-06-11T00:00:00+00:00"),        # newest resumable
        _row("c", "errored", "2026-06-09T00:00:00+00:00"),
        _row("d", "merge_blocked", "2026-06-08T00:00:00+00:00"),
    ]
    monkeypatch.setattr(m, "_status_rows", lambda control: rows)
    out = m._resumable_rows(tmp_path)
    assert [r[0] for r in out] == ["b", "c", "d"]                # running dropped, newest-first


def test_past_change_count_counts_completed_rows(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        m, "_completed_rows",
        lambda control: [_row("x", "completed", ""), _row("y", "abandoned", "")])
    assert m._past_change_count(tmp_path) == 2

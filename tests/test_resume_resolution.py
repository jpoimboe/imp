from __future__ import annotations

from pathlib import Path

import imp.cli.main as m
from imp.cli.store import CompletedRow, HomeDigest


# _Row = (id, status, step, last_iso, title, created_at)
def _row(cid: str, status: str, last: str) -> tuple[str, str, str, str, str, str]:
    return (cid, status, "plan", last, "Title " + cid, "2026-06-01T00:00:00+00:00")


def test_resumable_rows_drops_running_and_sorts_newest_first(
    monkeypatch, tmp_path: Path,
) -> None:
    rows = [
        _row("a", "running", "2026-06-10T00:00:00+00:00"),       # excluded (executing)
        _row("b", "paused", "2026-06-11T00:00:00+00:00"),        # newest resumable
        _row("c", "new", "2026-06-09T00:00:00+00:00"),          # resumable at-rest
        _row("d", "merge_blocked", "2026-06-08T00:00:00+00:00"),
    ]
    monkeypatch.setattr(m, "_status_rows", lambda control: rows)
    monkeypatch.setattr(m, "_backlog_rows", lambda control: [])
    out = m._resumable_rows(tmp_path)
    assert [r[0] for r in out] == ["b", "c", "d"]                # running dropped, newest-first


def test_resumable_rows_includes_backlog_rows(monkeypatch, tmp_path: Path) -> None:
    backlog_row = _row("s1", "idea", "2026-06-12T00:00:00+00:00")
    monkeypatch.setattr(m, "_status_rows", lambda control: [])
    monkeypatch.setattr(m, "_backlog_rows", lambda control: [backlog_row])
    out = m._resumable_rows(tmp_path)
    assert [r[0] for r in out] == ["s1"]


def test_past_quest_count_counts_completed_rows(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        m, "_completed_rows",
        lambda control: [_row("x", "completed", ""), _row("y", "abandoned", "")])
    assert m._past_quest_count(tmp_path) == 2


def _completed_row(cid: str, last: str) -> CompletedRow:
    return CompletedRow(cid, "completed", "plan", last, "Title " + cid, "2026-06-01T00:00:00+00:00")


class _StubStore:
    def __init__(self, homes, rows=()):
        self._homes = homes
        self._rows = rows

    def scan_homes(self) -> list[HomeDigest]:
        return list(self._homes)

    def completed_rows(self):
        return list(self._rows)


def test_iter_quest_ids_live_first_then_archived_sorted_deduped(
    monkeypatch, tmp_path: Path,
) -> None:
    # Live ids come through the store (scan_homes); their order is preserved, archived
    # ids follow sorted, and quest-a (in both) appears once.
    homes = [_make_home("quest-b", ""), _make_home("quest-a", "")]
    rows = [
        _completed_row("quest-a", "2026-06-01T00:00:00+00:00"),  # already live → deduped
        _completed_row("quest-c", "2026-06-01T00:00:00+00:00"),
        _completed_row("quest-d", "2026-06-01T00:00:00+00:00"),
    ]
    monkeypatch.setattr(m, "make_store", lambda _: _StubStore(homes, rows))
    assert m._iter_quest_ids(tmp_path) == ["quest-b", "quest-a", "quest-c", "quest-d"]

from __future__ import annotations

from pathlib import Path

import imp.cli.main as m
from tests.conftest import _seed_control
from tests.test_cli_list_filters import _archive_status


def test_history_by_disposition_selects_only_the_wanted_dispositions(
        tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "mrgd", "merged", last_ts="2026-06-10T00:00:00+00:00")
    _archive_status(tmp_path, "abnd", "abandoned", last_ts="2026-06-09T00:00:00+00:00")
    _archive_status(tmp_path, "shlv", "shelved", last_ts="2026-06-08T00:00:00+00:00")
    rows = m._history_by_disposition(
        tmp_path, dispositions={"abandoned", "shelved"}, live_ids=set())
    assert [r[0] for r in rows] == ["abnd", "shlv"]   # newest-first; merged excluded
    # The live-zone tokens name no archive status, so they select nothing.
    assert m._history_by_disposition(
        tmp_path, dispositions={"active", "backlog"}, live_ids=set()) == []


def test_history_by_disposition_dedups_rows_with_a_live_home(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "mrgd", "merged", last_ts="2026-06-10T00:00:00+00:00")
    assert m._history_by_disposition(
        tmp_path, dispositions={"merged"}, live_ids={"mrgd"}) == []

from __future__ import annotations

import json
from pathlib import Path

from imp.engine import archive
from imp.engine.archive import read as archive_read


def _archive(tmp_path: Path, change_id: str, title: str, status: str = "abandoned") -> None:
    events = tmp_path / f"{change_id}.jsonl"
    events.write_text("")
    change_json = tmp_path / f"{change_id}.change.json"
    change_json.write_text(json.dumps({
        "id": change_id, "title": title, "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    state_json = tmp_path / f"{change_id}.state.json"
    state_json.write_text(json.dumps({"id": change_id, "status": status, "step": "spec"}))
    archive.archive_change(
        tmp_path, change_id, events_path=events,
        change_json_path=change_json, state_json_path=state_json)


def test_search_archive_matches_title_substring(tmp_path: Path) -> None:
    _archive(tmp_path, "alpha", "Dark mode toggle")
    _archive(tmp_path, "beta", "Login flow")
    hits = archive_read.search_archive(tmp_path, "dark")
    assert [h[0] for h in hits] == ["alpha"]          # title filter, not a list-all
    assert hits[0][1] == "Dark mode toggle"           # title carried in the row
    assert hits[0][2] == "abandoned"                  # status carried in the row


def test_search_archive_matches_id_substring_case_insensitively(tmp_path: Path) -> None:
    _archive(tmp_path, "beta", "Login flow")
    assert [h[0] for h in archive_read.search_archive(tmp_path, "BET")] == ["beta"]


def test_search_archive_no_match_returns_empty(tmp_path: Path) -> None:
    _archive(tmp_path, "alpha", "Dark mode toggle")
    assert archive_read.search_archive(tmp_path, "zzz") == []


def test_search_archive_missing_index_returns_empty(tmp_path: Path) -> None:
    assert archive_read.search_archive(tmp_path, "anything") == []


def test_search_archive_treats_percent_as_literal(tmp_path: Path) -> None:
    # A `%` in the query must match a literal percent, not act as a wildcard.
    _archive(tmp_path, "alpha", "Dark mode toggle")
    _archive(tmp_path, "pct", "50% faster")
    _archive(tmp_path, "other", "50 items")
    assert [h[0] for h in archive_read.search_archive(tmp_path, "50%")] == ["pct"]

import json
from pathlib import Path

import pytest

from imp.cli.main import _iso_from_line, _journal_last_iso, _last_event_iso
from imp.engine import archive


@pytest.mark.parametrize(
    "line, expected",
    [
        ('{"timestamp":"2026-06-05T12:34:56+00:00"}', "2026-06-05T12:34:56+00:00"),
        ("", None),
        ("   ", None),
        ("not json", None),
        ('{"no":"timestamp"}', None),
    ],
)
def test_iso_from_line(line, expected):
    assert _iso_from_line(line) == expected


def test_last_event_iso_reads_last_event(tmp_path: Path):
    p = tmp_path / "events.jsonl"
    p.write_bytes(
        b'{"timestamp":"2026-06-01T00:00:00+00:00"}\n'
        b'{"timestamp":"2026-06-05T12:00:00+00:00"}\n'
    )
    assert _last_event_iso(p) == "2026-06-05T12:00:00+00:00"


def test_last_event_iso_missing_file(tmp_path: Path):
    assert _last_event_iso(tmp_path / "nope.jsonl") is None


def test_journal_last_iso_uses_last_line(tmp_path: Path):
    events = tmp_path / "e.jsonl"
    events.write_bytes(
        b'{"timestamp":"2026-06-01T00:00:00+00:00"}\n'
        b'{"timestamp":"2026-06-05T12:00:00+00:00"}\n'
    )
    cj = tmp_path / "c.json"
    cj.write_text(json.dumps({
        "id": "some-id", "title": "Some", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    sj = tmp_path / "s.json"
    sj.write_text(json.dumps({"id": "some-id", "status": "completed", "step": None}))
    archive.archive_change(
        tmp_path, "some-id", events_path=events,
        change_json_path=cj, state_json_path=sj)
    assert _journal_last_iso(tmp_path, "some-id") == "2026-06-05T12:00:00+00:00"


def test_journal_last_iso_absent(tmp_path: Path):
    assert _journal_last_iso(tmp_path, "x") is None

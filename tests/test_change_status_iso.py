from pathlib import Path

import pytest

from imp.cli import main as m
from imp.cli.main import _iso_from_line, _journal_last_iso, _last_event_iso


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


def test_journal_last_iso_uses_last_line(monkeypatch, tmp_path: Path):
    blob = (
        b'{"timestamp":"2026-06-01T00:00:00+00:00"}\n'
        b'{"timestamp":"2026-06-05T12:00:00+00:00"}\n'
    )
    monkeypatch.setattr(m.journal, "journal_show", lambda control, cid, name: blob)
    assert _journal_last_iso(tmp_path, "some-id") == "2026-06-05T12:00:00+00:00"


def test_journal_last_iso_absent(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(m.journal, "journal_show", lambda control, cid, name: None)
    assert _journal_last_iso(tmp_path, "x") is None

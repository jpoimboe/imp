from __future__ import annotations

import json

from imp.cli.main import _status_row


def _state(**kw: object) -> bytes:
    base = {"status": "running", "step": "write"}
    base.update(kw)
    return json.dumps(base).encode()


_QUEST = json.dumps(
    {"title": "T", "created_at": "2026-05-01T00:00:00+00:00"}).encode()


def test_status_row_carries_started_at_at_index_8() -> None:
    row = _status_row(
        "q1", _state(started_at="2026-06-01T00:00:00+00:00"), _QUEST,
        "2026-06-02T00:00:00+00:00")
    assert row is not None
    assert row[8] == "2026-06-01T00:00:00+00:00"


def test_status_row_started_at_is_none_when_absent() -> None:
    row = _status_row("q1", _state(), _QUEST, "2026-06-02T00:00:00+00:00")
    assert row is not None
    assert row[8] is None

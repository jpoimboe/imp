from __future__ import annotations

from types import SimpleNamespace

import pytest


class _FakeStore:
    """Minimal Store stand-in: quest_card yields a status, get_events yields events."""

    def __init__(self, events: list[dict], status: str) -> None:
        self._events = events
        self._status = status

    def get_events(self, quest_id: str) -> list[dict]:
        return self._events

    def quest_card(self, quest_id: str):
        return SimpleNamespace(status=self._status)

    def completed_rows(self):
        return []


_EVENTS = [
    {"id": "e1", "type": "step_started", "agent_id": None,
     "payload": {"step": "spec"}},
    {"id": "e2", "type": "step_completed", "agent_id": None,
     "payload": {"step": "spec", "status": "Success"}},
]


def test_summary_status_from_store_survives_masked_events(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`imp log <id> -s` reads status from quest_card, not the masked events.jsonl."""
    import imp.cli.main as m
    from imp.engine.quest import Quest
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    Quest.create(tmp_path, title="A", quest_id="alpha", base_branch="main")

    def _masked(*a: object, **k: object):
        raise PermissionError("events.jsonl is masked")

    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_row_for_id", _masked)
    monkeypatch.setattr(m, "make_store", lambda _c: _FakeStore(_EVENTS, "MERGED"))
    monkeypatch.setenv("NO_COLOR", "1")

    rc = m.main(["log", "alpha", "-s", "--no-pager"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "MERGED" in out


def test_export_summary_status_from_store_survives_masked_events(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`imp log <id> -s --md=-` reads status from quest_card, not the masked events.jsonl."""
    import imp.cli.main as m
    from imp.engine.quest import Quest
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    Quest.create(tmp_path, title="A", quest_id="alpha", base_branch="main")

    def _masked(*a: object, **k: object):
        raise PermissionError("events.jsonl is masked")

    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_row_for_id", _masked)
    monkeypatch.setattr(m, "make_store", lambda _c: _FakeStore(_EVENTS, "MERGED"))
    monkeypatch.setenv("NO_COLOR", "1")

    rc = m.main(["log", "alpha", "-s", "--md=-"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "**Status:** MERGED" in out


def test_html_export_summary_status_from_store_survives_masked_events(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`imp log <id> -s --html=-` reads status from quest_card, not the masked events.jsonl."""
    import imp.cli.main as m
    from imp.engine.quest import Quest
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    Quest.create(tmp_path, title="A", quest_id="alpha", base_branch="main")

    def _masked(*a: object, **k: object):
        raise PermissionError("events.jsonl is masked")

    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_row_for_id", _masked)
    monkeypatch.setattr(m, "make_store", lambda _c: _FakeStore(_EVENTS, "MERGED"))
    monkeypatch.setenv("NO_COLOR", "1")

    rc = m.main(["log", "alpha", "-s", "--html=-"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "MERGED" in out

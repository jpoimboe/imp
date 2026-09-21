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
        return SimpleNamespace(status=self._status, workflow="imp")

    def completed_rows(self):
        return []

    def scan_homes(self):
        # _live_quest_ids resolves live ids through the store; this file's only
        # seeded live home is "alpha".
        from imp.cli.store import HomeDigest
        return [HomeDigest("alpha", b"{}", None, {}, "", False, "", None, False)]


_EVENTS = [
    {"id": "e1", "type": "step_started", "agent_id": None,
     "payload": {"step": "spec"}},
    {"id": "e2", "type": "step_completed", "agent_id": None,
     "payload": {"step": "spec", "status": "Success"}},
]


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["log", "alpha", "-s", "--no-pager"], "MERGED"),
        (["log", "alpha", "-s", "--md=-"], "**Status:** MERGED"),
        (["log", "alpha", "-s", "--html=-"], "MERGED"),
    ],
    ids=["terminal", "md", "html"],
)
def test_summary_status_from_store_survives_masked_events(
    argv: list[str],
    expected: str,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`imp log <id> -s` (render paths) reads status from quest_card, not masked events.jsonl."""
    import imp.cli.main as m
    import imp.cli.store as s
    from imp.engine.quest import Quest
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    Quest.create(tmp_path, title="A", quest_id="alpha", base_branch="main")

    def _masked(*a: object, **k: object):
        raise PermissionError("events.jsonl is masked")

    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(s, "_row_for_id", _masked)
    monkeypatch.setattr("imp.cli.store.make_store", lambda _c: _FakeStore(_EVENTS, "MERGED"))
    monkeypatch.setenv("NO_COLOR", "1")

    rc = m.main(argv)
    out = capsys.readouterr().out
    assert rc == 0
    assert expected in out


class _CardlessStore(_FakeStore):
    """quest_card misses — the branch that already renders status `?`."""

    def quest_card(self, quest_id: str) -> None:
        return None


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["log", "alpha", "-s", "--no-pager"], "imp  ·  alpha  ·  ?"),
        (["log", "alpha", "-s", "--md=-"], "# imp — alpha"),
    ],
    ids=["terminal", "md"],
)
def test_a_missing_card_heads_with_the_project_default_workflow(
    argv: list[str],
    expected: str,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No card to source a workflow from: the project default is the only honest
    answer, and `_seed_control` pins it to `imp`."""
    import imp.cli.main as m
    import imp.cli.store as s
    from imp.engine.quest import Quest
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    Quest.create(tmp_path, title="A", quest_id="alpha", base_branch="main")

    def _masked(*a: object, **k: object):
        raise PermissionError("events.jsonl is masked")

    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(s, "_row_for_id", _masked)
    monkeypatch.setattr("imp.cli.store.make_store",
                        lambda _c: _CardlessStore(_EVENTS, "unused"))
    monkeypatch.setenv("NO_COLOR", "1")

    rc = m.main(argv)
    out = capsys.readouterr().out
    assert rc == 0
    assert expected in out


_INTERRUPTED_EVENTS = [
    {"type": "step_started", "timestamp": "2026-01-01T00:00:00+00:00",
     "payload": {"step": "task-lead", "phase": "implement"}},
    {"type": "tool_call", "id": "ev1", "agent_id": "lead1",
     "timestamp": "2026-01-01T00:00:10+00:00",
     "payload": {"name": "sub_agent", "id": "item_3", "id_ns": "block",
                 "input": {"subagent_name": "coder"}}},
    {"type": "step_completed", "timestamp": "2026-01-01T00:05:00+00:00",
     "payload": {"step": "task-lead", "action": {"verb": "continue"}}},
    {"type": "step_started", "timestamp": "2026-01-01T00:05:05+00:00",
     "payload": {"step": "verify", "phase": "implement"}},
]


@pytest.mark.parametrize(
    "argv,inferred_cell,interrupted_cell",
    [
        (["log", "alpha", "-s", "--no-pager"],
         "    ↳ coder      ~4m 50s   —    —     —",
         "  verify     interrupted   —    —     —"),
        (["log", "alpha", "-s", "--md=-"],
         "| ↳ coder | ~4m 50s | — | — | — | — | — |",
         "| verify | interrupted | — | — | — | — | — |"),
        (["log", "alpha", "-s", "--html=-"],
         "<td>~4m 50s</td>",
         "<td>interrupted</td>"),
    ],
    ids=["terminal", "md", "html"],
)
def test_archived_summary_marks_inferred_and_interrupted_spans(
    argv: list[str],
    inferred_cell: str,
    interrupted_cell: str,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Archived merged quest: contained subagent is `~`-marked; open step reads `interrupted`."""
    import imp.cli.main as m
    from imp.engine.quest import Quest
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    Quest.create(tmp_path, title="A", quest_id="alpha", base_branch="main")
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr("imp.cli.store.make_store",
                        lambda _c: _FakeStore(_INTERRUPTED_EVENTS, "merged"))
    monkeypatch.setenv("NO_COLOR", "1")

    rc = m.main(argv)
    out = capsys.readouterr().out
    assert rc == 0
    assert inferred_cell in out
    assert interrupted_cell in out      # the trailing `verify` step, never completed

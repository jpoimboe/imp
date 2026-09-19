from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import imp.cli.main as m
from imp.cli.store import NeighbourQuota, QuotaNeighboursUnsupported
from tests._quota_fixture import SUBJECT_ID, quota_events


class _QuotaStore:
    """The subject's events from the fixture; neighbours from `inner`."""

    def __init__(self, events: list[dict[str, Any]], inner: Any = None,
                 status: str = "merged") -> None:
        self._events = events
        self._inner = inner
        self._status = status
        self.asked: list[tuple[str, str, str]] = []

    def get_events(self, quest_id: str) -> list[dict[str, Any]]:
        return self._events

    def quest_card(self, quest_id: str) -> Any:
        return SimpleNamespace(status=self._status, title=quest_id)

    def scan_homes(self) -> list[Any]:
        return []

    def quota_neighbours(self, quest_id: str, start_iso: str,
                         end_iso: str) -> list[NeighbourQuota]:
        self.asked.append((quest_id, start_iso, end_iso))
        if self._inner is None:
            return []
        found: list[NeighbourQuota] = self._inner.quota_neighbours(
            quest_id, start_iso, end_iso)
        return found


def _cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: Any,
         quest_id: str = SUBJECT_ID) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_resolve_id", lambda control, token: quest_id)
    monkeypatch.setattr("imp.cli.store.make_store", lambda _c: store)
    monkeypatch.setenv("NO_COLOR", "1")


_METERED = [
    {"type": "step_started", "timestamp": "2026-01-01T00:00:00+00:00",
     "payload": {"step": "coder", "phase": "implement"}},
    {"type": "agent_message", "agent_id": "a1",
     "timestamp": "2026-01-01T00:00:10+00:00",
     "payload": {"provider": "claude", "model": "claude-sonnet-4-6",
                 "auth_mode": "apikey",
                 "usage": {"input_tokens": 1, "output_tokens": 1}}},
    {"type": "query_result", "agent_id": "a1",
     "timestamp": "2026-01-01T00:01:00+00:00",
     "payload": {"session_id": "s1", "duration_ms": 50000, "usage": {},
                 "model_usage": {"claude-sonnet-4-6": {
                     "inputTokens": 200000, "outputTokens": 9000,
                     "cacheReadInputTokens": 0,
                     "cacheCreationInputTokens": 0}}}},
    {"type": "step_completed", "timestamp": "2026-01-01T00:01:30+00:00",
     "payload": {"step": "coder", "action": {"verb": "continue"}}},
]


def test_a_metered_run_renders_todays_output_and_asks_no_neighbours(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    store = _QuotaStore(_METERED)
    _cli(tmp_path, monkeypatch, store)
    assert m.main(["log", SUBJECT_ID, "-s", "--no-pager"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1] == "           time      in   out    cost"
    assert lines[3] == "  coder  1m 30s  200.0k  9.0k  ~$0.73"
    assert lines[-1] == "  ~ estimated at API list price"
    assert not any("quota" in ln or "weekly" in ln for ln in lines)
    assert store.asked == []


@pytest.mark.parametrize("exc,text", [
    (OSError("no such socket"), "no such socket"),
    (RuntimeError("query failed"), "query failed"),
    (json.JSONDecodeError("Expecting value", "", 0),
     "Expecting value: line 1 column 1 (char 0)"),
], ids=["socket", "query-failed", "truncated-reply"])
def test_an_unreachable_store_fails_loudly_rather_than_undercounting(
        exc: Exception, text: str, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """The three shapes the seam produces: a dead socket, a daemon reporting
    failure, and a truncated reply, which `parse_result` raises as a
    `JSONDecodeError` (`src/imp/remote/session/protocol.py:225`)."""
    class _Broken(_QuotaStore):
        def quota_neighbours(self, *a: str) -> list[NeighbourQuota]:
            raise exc

    _cli(tmp_path, monkeypatch, _Broken(quota_events(SUBJECT_ID)))
    assert m.main(["log", SUBJECT_ID, "-s", "--no-pager"]) == 2
    captured = capsys.readouterr()
    assert captured.err == ("imp: cannot reach the quest store for quota "
                            f"attribution (quota_neighbours): {text}\n")
    assert "%" not in captured.out


def test_a_session_predating_the_op_keeps_the_cost_column(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    class _Old(_QuotaStore):
        def quota_neighbours(self, *a: str) -> list[NeighbourQuota]:
            raise QuotaNeighboursUnsupported("unknown store op")

    _cli(tmp_path, monkeypatch, _Old(quota_events(SUBJECT_ID)))
    assert m.main(["log", SUBJECT_ID, "-s", "--no-pager"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[-3] == "  total                 2h 6m   28.0M  531.1k  $42.20*"
    assert lines[-2] == ("  * theoretical API list price; actual subscription"
                         " usage is much lower")
    assert lines[-1] == ("  quota unavailable; this quest's session predates"
                         " the quota column")


def _codex_reading(ts: str, used_pct: float) -> dict[str, Any]:
    return {"type": "quota_reading", "timestamp": ts,
            "payload": {"provider": "codex", "plan": None,
                        "windows": [{"label": "7d", "used_pct": used_pct,
                                     "resets_at": 1789758000}]}}


def test_a_neighbour_on_another_provider_does_not_make_this_run_mixed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """The weekly gauge is account-global, so an overlapping codex quest's
    readings reach the attribution. samo71 ran no codex step, so its column is
    still one claude figure -- no dashed total, no breakdown, no codex legend."""
    class _WithCodex(_QuotaStore):
        def quota_neighbours(self, *a: str) -> list[NeighbourQuota]:
            return [NeighbourQuota(
                project="other", quest_id="zzzz11",
                readings=(_codex_reading("2026-09-18T03:40:00+00:00", 10.0),
                          _codex_reading("2026-09-18T05:00:00+00:00", 15.0)),
                usage=(), anchors=(), agents={})]

    _cli(tmp_path, monkeypatch, _WithCodex(quota_events(SUBJECT_ID)))
    assert m.main(["log", SUBJECT_ID, "-s", "--no-pager"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[-2] == "  total                 2h 6m   28.0M  531.1k   3.00%"
    assert lines[-1] == ("  * estimated to ±1 point of the weekly gauge; usage"
                         " outside imp during the run skews it high")
    assert not any("codex" in ln for ln in lines)


_MIXED_AUTH = [
    # Step 1: metered (apikey)
    {"type": "step_started", "timestamp": "2026-09-18T02:00:00+00:00",
     "payload": {"step": "spec", "phase": "plan"}},
    {"type": "agent_message", "agent_id": "b1",
     "timestamp": "2026-09-18T02:00:05+00:00",
     "payload": {"provider": "claude", "model": "claude-sonnet-4-6",
                 "auth_mode": "apikey",
                 "usage": {"input_tokens": 1, "output_tokens": 1}}},
    {"type": "query_result", "agent_id": "b1",
     "timestamp": "2026-09-18T02:01:00+00:00",
     "payload": {"session_id": "sx1", "duration_ms": 55000, "usage": {},
                 "model_usage": {"claude-sonnet-4-6": {
                     "inputTokens": 100000, "outputTokens": 5000,
                     "cacheReadInputTokens": 0,
                     "cacheCreationInputTokens": 0}}}},
    {"type": "step_completed", "timestamp": "2026-09-18T02:01:30+00:00",
     "payload": {"step": "spec", "action": {"verb": "continue"}}},
    # Step 2: notional (subscription — no auth_mode)
    {"type": "step_started", "timestamp": "2026-09-18T02:02:00+00:00",
     "payload": {"step": "coder", "phase": "implement"}},
    {"type": "agent_message", "agent_id": "b2",
     "timestamp": "2026-09-18T02:02:05+00:00",
     "payload": {"provider": "claude", "model": "claude-sonnet-4-6",
                 "usage": {"input_tokens": 1, "output_tokens": 1}}},
    {"type": "query_result", "agent_id": "b2",
     "timestamp": "2026-09-18T02:03:00+00:00",
     "payload": {"session_id": "sx2", "duration_ms": 55000, "usage": {},
                 "model_usage": {"claude-sonnet-4-6": {
                     "inputTokens": 80000, "outputTokens": 4000,
                     "cacheReadInputTokens": 0,
                     "cacheCreationInputTokens": 0}}}},
    {"type": "step_completed", "timestamp": "2026-09-18T02:03:30+00:00",
     "payload": {"step": "coder", "action": {"verb": "continue"}}},
    # Two quota readings spanning the run so the gauge resolves
    {"type": "quota_reading", "timestamp": "2026-09-18T02:00:30+00:00",
     "payload": {"provider": "claude", "plan": None,
                 "windows": [{"label": "7d", "used_pct": 40.0,
                              "resets_at": 1789758000}]}},
    {"type": "quota_reading", "timestamp": "2026-09-18T02:03:00+00:00",
     "payload": {"provider": "claude", "plan": None,
                 "windows": [{"label": "7d", "used_pct": 43.0,
                              "resets_at": 1789758000}]}},
]


def test_a_run_mixing_apikey_and_subscription_steps_still_gets_the_column(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """The metered gate asks whether ANY row is notional."""
    store = _QuotaStore(_MIXED_AUTH)
    _cli(tmp_path, monkeypatch, store)
    assert m.main(["log", SUBJECT_ID, "-s", "--no-pager"]) == 0
    lines = capsys.readouterr().out.splitlines()
    # Two-row header: "tokens" above, "weekly*" in the second row
    assert any("weekly" in ln for ln in lines[:4])
    # Total row ends with a % cell
    total_line = next(ln for ln in lines if ln.strip().startswith("total"))
    assert total_line.rstrip().endswith("%")
    # No dollar amounts anywhere
    assert not any("$" in ln for ln in lines)
    assert not any("~" in ln for ln in lines)

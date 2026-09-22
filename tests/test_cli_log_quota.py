from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

import imp.cli.main as m
from imp.cli.quota_column import QuotaColumn, share_cell
from imp.cli.quota_share import QuotaFigure
from imp.cli.render_summary import (
    SummaryHeader,
    build_summary,
    exclusion_spans,
    render_summary,
    summary_total,
)
from imp.cli.store import LocalStore, NeighbourQuota
from imp.cli.store_proxy import ProxyStore, run_query
from imp.remote.session.socket_server import SessionSocket
from tests._quota_fixture import NEIGHBOUR_ID, SUBJECT_ID, quota_events


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
        return SimpleNamespace(status=self._status, title=quest_id,
                               workflow="default")

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


class _ResetGauge(_QuotaStore):
    """A neighbour whose claude 7d sample stamps a resets_at the samo71 fixture never does."""

    def quota_neighbours(self, *a: str) -> list[NeighbourQuota]:
        return [NeighbourQuota(
            project="other", quest_id="zzzz11",
            readings=({"type": "quota_reading",
                       "timestamp": "2026-09-18T05:00:00+00:00",
                       "payload": {"provider": "claude", "plan": None,
                                   "windows": [{"label": "7d", "used_pct": 3.0,
                                                "resets_at": 1789844400}]}},),
            usage=(), anchors=(), agents={})]


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


def test_a_withheld_gauge_keeps_the_cost_column(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """No gauge resolved, so `imp log -s` keeps the dollar column and gains the
    one line naming why."""
    _cli(tmp_path, monkeypatch, _ResetGauge(quota_events(SUBJECT_ID)))
    assert m.main(["log", SUBJECT_ID, "-s", "--no-pager"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[-3] == "  total                 2h 6m   28.0M  531.1k  $42.20*"
    assert lines[-2] == ("  * theoretical API list price; actual subscription"
                         " usage is much lower")
    assert lines[-1] == ("  quota unmeasurable; the weekly window reset during"
                         " this run")


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


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _neighbour_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fego87's replay as a live quest home in another project."""
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    qdir = tmp_path / "imphome" / "projects" / "other" / "quests" / NEIGHBOUR_ID
    qdir.mkdir(parents=True)
    (qdir / "quest.json").write_text(json.dumps(
        {"id": NEIGHBOUR_ID, "title": NEIGHBOUR_ID, "base_branch": "main",
         "created_at": "2026-09-18T03:00:00+00:00"}))
    (qdir / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in quota_events(NEIGHBOUR_ID)))


def test_samo71_renders_its_share_of_the_week(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    _neighbour_home(tmp_path, monkeypatch)
    store = _QuotaStore(quota_events(SUBJECT_ID), LocalStore(tmp_path))
    _cli(tmp_path, monkeypatch, store)
    assert m.main(["log", SUBJECT_ID, "-s", "--no-pager"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1] == "                                   tokens       quota"
    assert lines[2] == "                         time      in     out  weekly*"
    assert lines[4] == "  threshold             5m 5s    1.4M   27.8k   0.19%"
    assert lines[-2] == "  total                 2h 6m   28.0M  531.1k   2.83%"
    assert lines[-1] == ("  * estimated to ±1 point of the weekly gauge; usage"
                         " outside imp during the run skews it high")
    assert not any("$" in ln for ln in lines)
    # The span asked for is the quest's first event to its last.
    assert store.asked == [(SUBJECT_ID, "2026-09-18T03:29:57.904744+00:00",
                            "2026-09-18T05:38:03.485364+00:00")]


def _figure(store: Any, events: list[dict[str, Any]]) -> QuotaFigure:
    rows = build_summary(events)
    view = m._attribute_quota(store, SUBJECT_ID, events, rows)
    assert isinstance(view, QuotaColumn)
    figure = view.gauges[0]
    assert isinstance(figure, QuotaFigure)
    return figure


def test_the_per_step_cells_sum_to_the_quest_total(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _neighbour_home(tmp_path, monkeypatch)
    events = quota_events(SUBJECT_ID)
    rows = build_summary(events)
    view = m._attribute_quota(LocalStore(tmp_path), SUBJECT_ID, events, rows)
    assert isinstance(view, QuotaColumn)
    figure = view.gauges[0]
    assert isinstance(figure, QuotaFigure)
    assert share_cell(figure.percent) == "2.83%"
    # Numerator and denominator are the same row-attributed quantity, so the
    # rendered cells sum to the total rather than approximately to it.
    assert sum(figure.scale(r.subtree_units) for r in rows) == pytest.approx(
        figure.percent, abs=1e-9)


def test_discovery_routes_through_the_store_not_the_filesystem(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _neighbour_home(tmp_path, monkeypatch)
    events = quota_events(SUBJECT_ID)
    # fego87 drew 1.8338 of the 31.7793 in-span units, so reaching it moves the
    # figure off the 3.00% a store-less walk would report. A direct filesystem
    # read in-sandbox returns a partial neighbour set, so the guard is that the
    # denominator provably comes from the store.
    assert share_cell(_figure(LocalStore(tmp_path), events).percent) == "2.83%"
    assert share_cell(_figure(_QuotaStore(events, None),
                              events).percent) == "3.00%"


def test_a_filtered_render_keeps_the_steps_own_cell(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    _neighbour_home(tmp_path, monkeypatch)
    store = _QuotaStore(quota_events(SUBJECT_ID), LocalStore(tmp_path))
    _cli(tmp_path, monkeypatch, store)
    assert m.main(["log", SUBJECT_ID, "-s", "--no-pager"]) == 0
    full = capsys.readouterr().out
    assert m.main(["log", SUBJECT_ID, "-s", "--step", "threshold",
                   "--no-pager"]) == 0
    filtered = capsys.readouterr().out
    cells = [next(ln for ln in text.splitlines()
                  if ln.startswith("  threshold ")).split()[-1]
             for text in (full, filtered)]
    assert cells == ["0.19%", "0.19%"]
    # Every column of the totals row is the subtotal of the rows above it, the
    # share included: 2.83% of the week unfiltered, this step's 0.19% under it.
    totals = [next(ln for ln in text.splitlines() if ln.startswith("  total"))
              for text in (full, filtered)]
    assert totals == ["  total                 2h 6m   28.0M  531.1k   2.83%",
                      "  total          5m 5s    1.4M  27.8k   0.19%"]


@pytest.mark.anyio
async def test_local_and_proxied_neighbours_render_the_same_bytes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _neighbour_home(tmp_path, monkeypatch)
    events = quota_events(SUBJECT_ID)

    def _render(store: Any) -> list[str]:
        rows = build_summary(events)
        view = m._attribute_quota(store, SUBJECT_ID, events, rows)
        assert not isinstance(view, int)
        header = SummaryHeader(workflow="default", quest_id=SUBJECT_ID,
                               status="merged",
                               total_seconds=summary_total(events),
                               exclusions=exclusion_spans(events))
        return render_summary(rows, header=header, color=False, width=100,
                              quota=view)

    local = LocalStore(tmp_path)
    sock = str(tmp_path / "s.sock")

    async def on_query(op: str, args: dict[str, Any]) -> object:
        return run_query(local, op, args)

    session = SessionSocket(sock, on_query=on_query)
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)
        direct = _render(local)
        proxied = await anyio.to_thread.run_sync(_render, ProxyStore(sock))
        assert proxied == direct
        assert direct[-2].endswith("  2.83%")
        await session.close()
        tg.cancel_scope.cancel()


_MD_QUOTA_HEADER = ("| Step | Duration | Fresh | Cache read | Cache write | Out"
                    " | Weekly* |")


def test_the_markdown_export_carries_samo71s_share(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """AC 19(a) end to end: the same 2.83% the terminal renders."""
    _neighbour_home(tmp_path, monkeypatch)
    store = _QuotaStore(quota_events(SUBJECT_ID), LocalStore(tmp_path))
    _cli(tmp_path, monkeypatch, store)
    assert m.main(["log", SUBJECT_ID, "-s", "--md=-"]) == 0
    out = capsys.readouterr().out
    assert _MD_QUOTA_HEADER in out
    assert "| threshold | 5m 5s | 13.4k | 1.2M | 199.2k | 27.8k | 0.19% |" in out
    assert ("| total | 2h 6m | 174.1k | 25.8M | 2.0M | 531.1k | 2.83% |"
            in out)
    assert ("\\* estimated to ±1 point of the weekly gauge; usage outside imp"
            " during the run skews it high") in out
    assert "$" not in out


def test_a_filtered_export_keeps_the_steps_own_cell(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """AC 19(b): the export gets the filtered rows, so it needs the unfiltered
    log to attribute over — or this step's share would be computed against a
    span and a denominator that the filter moved."""
    _neighbour_home(tmp_path, monkeypatch)
    store = _QuotaStore(quota_events(SUBJECT_ID), LocalStore(tmp_path))
    _cli(tmp_path, monkeypatch, store)
    assert m.main(["log", SUBJECT_ID, "-s", "--step", "threshold",
                   "--md=-"]) == 0
    out = capsys.readouterr().out
    assert "| threshold | 5m 5s | 13.4k | 1.2M | 199.2k | 27.8k | 0.19% |" in out
    assert ("| total | 5m 5s | 13.4k | 1.2M | 199.2k | 27.8k | 0.19% |" in out)
    assert "2.83%" not in out          # the quest total belongs to the full run


def test_the_html_export_carries_the_share_as_a_table_cell(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    _neighbour_home(tmp_path, monkeypatch)
    store = _QuotaStore(quota_events(SUBJECT_ID), LocalStore(tmp_path))
    _cli(tmp_path, monkeypatch, store)
    assert m.main(["log", SUBJECT_ID, "-s", "--html=-"]) == 0
    out = capsys.readouterr().out
    assert "<th>Weekly*</th>" in out
    assert "<td>2.83%</td>" in out
    assert "<td>0.19%</td>" in out


def test_a_metered_export_is_unchanged_and_asks_no_neighbours(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """AC 12 on the export surface: today's bytes, no added legend line."""
    store = _QuotaStore(_METERED)
    _cli(tmp_path, monkeypatch, store)
    assert m.main(["log", SUBJECT_ID, "-s", "--md=-"]) == 0
    out = capsys.readouterr().out
    assert ("| Step | Duration | Fresh | Cache read | Cache write | Out |"
            " Cost |") in out
    assert "| coder | 1m 30s | 200.0k | — | — | 9.0k | ~$0.73 |" in out
    assert "| total | 1m 30s | 200.0k | — | — | 9.0k | ~$0.73 |" in out
    assert out.rstrip("\n").endswith("~ estimated at API list price")
    assert "Weekly" not in out and "quota" not in out
    assert store.asked == []


def test_a_withheld_gauge_exports_the_cost_column_and_its_legend(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """AC 19(a) carries AC 6's fallbacks onto the export too: no gauge resolved,
    so the document keeps the dollar column and gains the line naming why."""
    _cli(tmp_path, monkeypatch, _ResetGauge(quota_events(SUBJECT_ID)))
    assert m.main(["log", SUBJECT_ID, "-s", "--md=-"]) == 0
    out = capsys.readouterr().out
    assert ("| Step | Duration | Fresh | Cache read | Cache write | Out |"
            " Cost |") in out
    assert "| total | 2h 6m | 174.1k | 25.8M | 2.0M | 531.1k | $42.20* |" in out
    star = ("\\* theoretical API list price; actual subscription usage is much"
            " lower")
    reset = "quota unmeasurable; the weekly window reset during this run"
    assert star in out and reset in out
    assert out.index(star) < out.index(reset)
    assert "Weekly" not in out and "%" not in out


def test_an_ordinal_range_export_keeps_the_steps_own_cell(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """AC 19(b)'s other selector. Events 1-107 are the threshold step — its
    `step_completed` is the 107th event of the fixture."""
    _neighbour_home(tmp_path, monkeypatch)
    store = _QuotaStore(quota_events(SUBJECT_ID), LocalStore(tmp_path))
    _cli(tmp_path, monkeypatch, store)
    assert m.main(["log", SUBJECT_ID, "-s", "--range=1-107", "--md=-"]) == 0
    out = capsys.readouterr().out
    assert "| threshold | 5m 5s | 13.4k | 1.2M | 199.2k | 27.8k | 0.19% |" in out
    # The range keeps the two events before the step started, so the total's
    # duration spans marginally wider than the step row's; the share does not.
    total = next(ln for ln in out.splitlines() if ln.startswith("| total |"))
    assert total.split("|")[-2].strip() == "0.19%"
    assert "2.83%" not in out


def test_an_unreachable_store_fails_the_export_too(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """AC 10 on the export surface: no document is written over an
    under-counted denominator."""
    class _Broken(_QuotaStore):
        def quota_neighbours(self, *a: str) -> list[NeighbourQuota]:
            raise OSError("no such socket")

    _cli(tmp_path, monkeypatch, _Broken(quota_events(SUBJECT_ID)))
    assert m.main(["log", SUBJECT_ID, "-s", "--md=-"]) == 2
    captured = capsys.readouterr()
    assert captured.err == ("imp: cannot reach the quest store for quota "
                            "attribution (quota_neighbours): no such socket\n")
    assert captured.out == ""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

import imp.cli.main as m
from imp.cli import quota_cache
from imp.cli.quota_cache import cache_dir
from imp.cli.quota_column import FOOTNOTE, LEGEND_BELOW, LEGEND_UNREAD, QuotaColumn
from imp.cli.quota_scan import scan_weeks
from imp.cli.quota_share import extract, gauge_weeks
from imp.cli.render_summary import (
    SummaryHeader,
    build_summary,
    exclusion_spans,
    render_summary,
    rows_charge,
    summary_total,
)
from imp.cli.store import GaugeWeek, LocalStore
from imp.cli.store_proxy import ProxyStore, run_query
from imp.engine.quest import LAYOUT_VERSION
from imp.paths import project_state_dir
from imp.remote.session.socket_server import SessionSocket
from tests._quota_fixture import NEIGHBOUR_ID, SUBJECT_ID, quota_events

_RESET = 1789758000
_STAR = "  * theoretical API list price; actual subscription usage is much lower"
_STAR_MD = "\\* theoretical API list price; actual subscription usage is much lower"


class _QuotaStore:
    """The subject's events from the fixture. Its weeks come from its own log
    alone, or from a real scan of every project under `root`."""

    def __init__(self, events: list[dict[str, Any]], root: Path | None = None,
                 status: str = "completed") -> None:
        self._events = events
        self._root = root
        self._status = status
        self.asked: list[str] = []

    def get_events(self, quest_id: str) -> list[dict[str, Any]]:
        return self._events

    def quest_card(self, quest_id: str) -> Any:
        return SimpleNamespace(status=self._status, title=quest_id,
                               intent=None, seed_opening=None)

    def scan_homes(self) -> list[Any]:
        return []

    def quota_weeks(self, quest_id: str) -> list[GaugeWeek]:
        self.asked.append(quest_id)
        if self._root is None:
            return gauge_weeks(extract(self._events), [])
        return scan_weeks(self._root, quest_id, self._events)


class _NoWeeks(_QuotaStore):
    """State 3: no reading anchors a week covering the run."""

    def quota_weeks(self, quest_id: str) -> list[GaugeWeek]:
        return []


class _Below(_QuotaStore):
    """State 2: the run's week has not ticked off zero."""

    def quota_weeks(self, quest_id: str) -> list[GaugeWeek]:
        return [GaugeWeek("claude", _RESET, 0.0, 0.0)]


def _cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: Any,
         quest_id: str = SUBJECT_ID) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_resolve_id", lambda control, token: quest_id)
    monkeypatch.setattr("imp.cli.store.make_store", lambda _c: store)
    monkeypatch.setenv("NO_COLOR", "1")


def _quest_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fego87's replay as a live quest home in another project."""
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    qdir = tmp_path / "imphome" / "projects" / "other" / "quests" / NEIGHBOUR_ID
    qdir.mkdir(parents=True)
    (qdir / "quest.json").write_text(json.dumps(
        {"id": NEIGHBOUR_ID, "title": NEIGHBOUR_ID, "base_branch": "main",
         "created_at": "2026-09-18T03:00:00+00:00"}))
    (qdir / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in quota_events(NEIGHBOUR_ID)))


def _subject_home(tmp_path: Path) -> None:
    """samo71 as a live quest of the project at `tmp_path`, so a LocalStore there
    reads its log. Call after `_quest_home`, which pins IMP_HOME."""
    from imp.engine.state import State

    qdir = project_state_dir(tmp_path) / "quests" / SUBJECT_ID
    (qdir / "state").mkdir(parents=True)
    (qdir / "quest.json").write_text(json.dumps(
        {"id": SUBJECT_ID, "title": SUBJECT_ID, "base_branch": "main",
         "created_at": "2026-09-18T03:00:00+00:00",
         "layout_version": LAYOUT_VERSION}))
    (qdir / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in quota_events(SUBJECT_ID)))
    State(id=SUBJECT_ID, status="paused", step="threshold",
          step_attempt=0).save(qdir / "state")


def _total(store: Any, events: list[dict[str, Any]]) -> str:
    rows = build_summary(events)
    view = m._attribute_quota(store, SUBJECT_ID, events, rows)
    assert isinstance(view, QuotaColumn)
    return view.total(rows_charge(rows))


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


def test_a_metered_run_renders_todays_output_and_scans_no_weeks(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """State 4."""
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
    `JSONDecodeError`."""
    class _Broken(_QuotaStore):
        def quota_weeks(self, quest_id: str) -> list[GaugeWeek]:
            raise exc

    _cli(tmp_path, monkeypatch, _Broken(quota_events(SUBJECT_ID)))
    assert m.main(["log", SUBJECT_ID, "-s", "--no-pager"]) == 2
    captured = capsys.readouterr()
    assert captured.err == ("imp: cannot reach the quest store for quota "
                            f"attribution (quota_weeks): {text}\n")
    assert "%" not in captured.out


def test_no_readings_for_the_week_keeps_the_cost_column(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """State 3: `imp log -s` keeps the dollar column and gains the line naming why."""
    _cli(tmp_path, monkeypatch, _NoWeeks(quota_events(SUBJECT_ID)))
    assert m.main(["log", SUBJECT_ID, "-s", "--no-pager"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[-3] == "  total                 2h 6m   28.0M  531.1k  $42.20*"
    assert lines[-2] == _STAR
    assert lines[-1] == f"  {LEGEND_UNREAD}"


def test_an_unmoved_week_reads_under_one_point(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """State 2: every row reads `—`, the total `<1%`."""
    _cli(tmp_path, monkeypatch, _Below(quota_events(SUBJECT_ID)))
    assert m.main(["log", SUBJECT_ID, "-s", "--no-pager"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[4].split()[-1] == "—"
    assert lines[-3].split() == ["total", "2h", "6m", "28.0M", "531.1k", "<1%"]
    assert lines[-2] == f"  {FOOTNOTE}"
    assert lines[-1] == f"  {LEGEND_BELOW}"
    assert not any("$" in ln for ln in lines)


def test_a_week_on_a_gauge_this_run_never_drew_on_does_not_make_it_mixed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """The weekly gauge is account-global, so a codex week reaches the store's
    answer. samo71 ran no codex step, so its column is still one claude figure:
    no dashed total, no breakdown, no codex legend."""
    class _WithCodex(_QuotaStore):
        def quota_weeks(self, quest_id: str) -> list[GaugeWeek]:
            return [*super().quota_weeks(quest_id),
                    GaugeWeek("codex", _RESET, 15.0, 0.0)]

    _cli(tmp_path, monkeypatch, _WithCodex(quota_events(SUBJECT_ID)))
    assert m.main(["log", SUBJECT_ID, "-s", "--no-pager"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[-2] == "  total                 2h 6m   28.0M  531.1k  81.00%"
    assert lines[-1] == f"  {FOOTNOTE}"
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
    {"type": "quota_reading", "timestamp": "2026-09-18T02:00:30+00:00",
     "payload": {"provider": "claude", "plan": None,
                 "windows": [{"label": "7d", "used_pct": 40.0,
                              "resets_at": 1789758000}]}},
    {"type": "quota_reading", "timestamp": "2026-09-18T02:03:00+00:00",
     "payload": {"provider": "claude", "plan": None,
                 "windows": [{"label": "7d", "used_pct": 43.0,
                              "resets_at": 1789758000}]}},
]


def test_a_metered_step_in_a_subscription_run_reads_dash_and_adds_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """AC 5(h) / AC 6: the metered gate asks whether ANY row is notional. The
    coder's 0.30 u are the week's only subscription units, so 43 points over
    them is 43.00%; the apikey spec step reads `—` and is in no denominator."""
    _cli(tmp_path, monkeypatch, _QuotaStore(_MIXED_AUTH))
    assert m.main(["log", SUBJECT_ID, "-s", "--no-pager"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert any("weekly" in ln for ln in lines[:4])
    cell = {ln.split()[0]: ln.split()[-1] for ln in lines
            if ln.split()[:1] in (["spec"], ["coder"], ["total"])}
    assert cell == {"spec": "—", "coder": "43.00%", "total": "43.00%"}
    assert not any("$" in ln or "~" in ln for ln in lines)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_samo71_renders_its_share_of_the_week(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    _quest_home(tmp_path, monkeypatch)
    store = _QuotaStore(quota_events(SUBJECT_ID), tmp_path)
    _cli(tmp_path, monkeypatch, store)
    assert m.main(["log", SUBJECT_ID, "-s", "--no-pager"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1] == "                                   tokens       quota"
    assert lines[2] == "                         time      in     out  weekly*"
    assert lines[4] == "  threshold             5m 5s    1.4M   27.8k   4.45%"
    assert lines[-2] == "  total                 2h 6m   28.0M  531.1k  67.68%"
    assert lines[-1] == f"  {FOOTNOTE}"
    assert not any("$" in ln for ln in lines)
    assert store.asked == [SUBJECT_ID]


def test_the_per_step_cells_sum_to_the_quest_total(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC 5(d): every row is priced at the one week's rate, and the total is the
    rows' sum, so the cells add up to it exactly."""
    _quest_home(tmp_path, monkeypatch)
    events = quota_events(SUBJECT_ID)
    rows = build_summary(events)
    view = m._attribute_quota(_QuotaStore(events, tmp_path), SUBJECT_ID, events, rows)
    assert isinstance(view, QuotaColumn)
    ((gauge, run),) = view.run.per_gauge
    assert gauge == "claude" and run.points is not None
    assert round(run.points, 2) == 67.68
    row_points = [c.points or 0.0 for r in rows for _g, c in r.subtree_charge.per_gauge]
    assert sum(row_points) == pytest.approx(run.points, abs=1e-9)


def test_discovery_routes_through_the_store_not_the_filesystem(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reaching fego87 moves the figure off the 81.00% the subject alone gives.
    A direct filesystem read in-sandbox returns a partial set of quests, so the
    guard is that the denominator provably comes from the store."""
    _quest_home(tmp_path, monkeypatch)
    _subject_home(tmp_path)
    events = quota_events(SUBJECT_ID)
    assert _total(LocalStore(tmp_path), events) == "67.68%"
    assert _total(_QuotaStore(events), events) == "81.00%"


def test_a_filtered_render_keeps_the_steps_own_cell(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    _quest_home(tmp_path, monkeypatch)
    _cli(tmp_path, monkeypatch, _QuotaStore(quota_events(SUBJECT_ID), tmp_path))
    assert m.main(["log", SUBJECT_ID, "-s", "--no-pager"]) == 0
    full = capsys.readouterr().out
    assert m.main(["log", SUBJECT_ID, "-s", "--step", "threshold",
                   "--no-pager"]) == 0
    filtered = capsys.readouterr().out
    cells = [next(ln for ln in text.splitlines()
                  if ln.startswith("  threshold ")).split()[-1]
             for text in (full, filtered)]
    assert cells == ["4.45%", "4.45%"]
    # Every column of the totals row is the subtotal of the rows above it, the
    # share included.
    totals = [next(ln for ln in text.splitlines() if ln.startswith("  total"))
              for text in (full, filtered)]
    assert totals == ["  total                 2h 6m   28.0M  531.1k  67.68%",
                      "  total          5m 5s    1.4M  27.8k   4.45%"]


@pytest.mark.anyio
async def test_local_and_proxied_weeks_render_the_same_bytes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC 8."""
    _quest_home(tmp_path, monkeypatch)
    _subject_home(tmp_path)
    events = quota_events(SUBJECT_ID)

    def _render(store: Any) -> list[str]:
        rows = build_summary(events)
        view = m._attribute_quota(store, SUBJECT_ID, events, rows)
        assert not isinstance(view, int)
        header = SummaryHeader(title="default", quest_id=SUBJECT_ID,
                               status="completed",
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
        assert next(ln for ln in direct if ln.startswith("  total")).endswith("  67.68%")
        await session.close()
        tg.cancel_scope.cancel()


_MD_QUOTA_HEADER = ("| Step | Duration | Fresh | Cache read | Cache write | Out"
                    " | Weekly* |")
_MD_COST_HEADER = ("| Step | Duration | Fresh | Cache read | Cache write | Out |"
                   " Cost |")
_THRESHOLD_MD = "| threshold | 5m 5s | 13.4k | 1.2M | 199.2k | 27.8k | 4.45% |"


def test_the_markdown_export_carries_samo71s_share(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """The same 67.68% the terminal renders."""
    _quest_home(tmp_path, monkeypatch)
    _cli(tmp_path, monkeypatch, _QuotaStore(quota_events(SUBJECT_ID), tmp_path))
    assert m.main(["log", SUBJECT_ID, "-s", "--md=-"]) == 0
    out = capsys.readouterr().out
    assert _MD_QUOTA_HEADER in out
    assert _THRESHOLD_MD in out
    assert "| total | 2h 6m | 174.1k | 25.8M | 2.0M | 531.1k | 67.68% |" in out
    assert f"\\{FOOTNOTE}" in out
    assert "$" not in out


def test_a_filtered_export_keeps_the_steps_own_cell(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """The export gets the filtered rows, so it needs the unfiltered log to price
    over."""
    _quest_home(tmp_path, monkeypatch)
    _cli(tmp_path, monkeypatch, _QuotaStore(quota_events(SUBJECT_ID), tmp_path))
    assert m.main(["log", SUBJECT_ID, "-s", "--step", "threshold",
                   "--md=-"]) == 0
    out = capsys.readouterr().out
    assert _THRESHOLD_MD in out
    assert "| total | 5m 5s | 13.4k | 1.2M | 199.2k | 27.8k | 4.45% |" in out
    assert "67.68%" not in out          # the quest total belongs to the full run


def test_the_html_export_carries_the_share_as_a_table_cell(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    _quest_home(tmp_path, monkeypatch)
    _cli(tmp_path, monkeypatch, _QuotaStore(quota_events(SUBJECT_ID), tmp_path))
    assert m.main(["log", SUBJECT_ID, "-s", "--html=-"]) == 0
    out = capsys.readouterr().out
    assert "<th>Weekly*</th>" in out
    assert "<td>67.68%</td>" in out
    assert "<td>4.45%</td>" in out


def test_a_metered_export_is_unchanged_and_scans_no_weeks(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """State 4 on the export surface: today's bytes, no added legend line."""
    store = _QuotaStore(_METERED)
    _cli(tmp_path, monkeypatch, store)
    assert m.main(["log", SUBJECT_ID, "-s", "--md=-"]) == 0
    out = capsys.readouterr().out
    assert _MD_COST_HEADER in out
    assert "| coder | 1m 30s | 200.0k | — | — | 9.0k | ~$0.73 |" in out
    assert "| total | 1m 30s | 200.0k | — | — | 9.0k | ~$0.73 |" in out
    assert out.rstrip("\n").endswith("~ estimated at API list price")
    assert "Weekly" not in out and "quota" not in out
    assert store.asked == []


def test_no_readings_export_the_cost_column_and_its_legend(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """State 3 on the export."""
    _cli(tmp_path, monkeypatch, _NoWeeks(quota_events(SUBJECT_ID)))
    assert m.main(["log", SUBJECT_ID, "-s", "--md=-"]) == 0
    out = capsys.readouterr().out
    assert _MD_COST_HEADER in out
    assert "| total | 2h 6m | 174.1k | 25.8M | 2.0M | 531.1k | $42.20* |" in out
    assert out.index(_STAR_MD) < out.index(LEGEND_UNREAD)
    assert "Weekly" not in out and "%" not in out


def test_an_unmoved_week_exports_under_one_point(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """State 2 on both exports."""
    _cli(tmp_path, monkeypatch, _Below(quota_events(SUBJECT_ID)))
    assert m.main(["log", SUBJECT_ID, "-s", "--md=-"]) == 0
    out = capsys.readouterr().out
    assert _MD_QUOTA_HEADER in out
    assert "| total | 2h 6m | 174.1k | 25.8M | 2.0M | 531.1k | <1% |" in out
    assert out.index(f"\\{FOOTNOTE}") < out.index(LEGEND_BELOW)
    assert m.main(["log", SUBJECT_ID, "-s", "--html=-"]) == 0
    html = capsys.readouterr().out
    assert "<td>&lt;1%</td>" in html
    assert f"<p>{LEGEND_BELOW}</p>" in html


def test_an_ordinal_range_export_keeps_the_steps_own_cell(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """The other selector. Events 1-107 are the threshold step — its
    `step_completed` is the 107th event of the fixture."""
    _quest_home(tmp_path, monkeypatch)
    _cli(tmp_path, monkeypatch, _QuotaStore(quota_events(SUBJECT_ID), tmp_path))
    assert m.main(["log", SUBJECT_ID, "-s", "--range=1-107", "--md=-"]) == 0
    out = capsys.readouterr().out
    assert _THRESHOLD_MD in out
    # The range keeps the two events before the step started, so the total's
    # duration spans marginally wider than the step row's; the share does not.
    total = next(ln for ln in out.splitlines() if ln.startswith("| total |"))
    assert total.split("|")[-2].strip() == "4.45%"
    assert "67.68%" not in out


def test_an_unreachable_store_fails_the_export_too(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """No document is written over an under-counted denominator."""
    class _Broken(_QuotaStore):
        def quota_weeks(self, quest_id: str) -> list[GaugeWeek]:
            raise OSError("no such socket")

    _cli(tmp_path, monkeypatch, _Broken(quota_events(SUBJECT_ID)))
    assert m.main(["log", SUBJECT_ID, "-s", "--md=-"]) == 2
    captured = capsys.readouterr()
    assert captured.err == ("imp: cannot reach the quest store for quota "
                            "attribution (quota_weeks): no such socket\n")
    assert captured.out == ""


def test_a_warm_cache_renders_the_same_bytes_as_a_cold_one(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """AC 9(b): the warm render parses no neighbour log at all."""
    _quest_home(tmp_path, monkeypatch)
    _cli(tmp_path, monkeypatch, _QuotaStore(quota_events(SUBJECT_ID), tmp_path))
    assert m.main(["log", SUBJECT_ID, "-s", "--no-pager"]) == 0
    cold = capsys.readouterr().out
    assert "  total                 2h 6m   28.0M  531.1k  67.68%" in cold.splitlines()
    assert len(list(cache_dir().glob("*.json"))) == 1

    def _boom(_raw: bytes) -> list[dict[str, Any]]:
        raise AssertionError("a warm render re-read a neighbour's log")

    monkeypatch.setattr(quota_cache, "_parse_jsonl", _boom)
    assert m.main(["log", SUBJECT_ID, "-s", "--no-pager"]) == 0
    assert capsys.readouterr().out == cold


@pytest.mark.parametrize("damage", ["corrupt", "delete"])
def test_a_damaged_cache_is_rebuilt_silently_with_the_same_bytes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str], damage: str) -> None:
    """AC 9(c)."""
    _quest_home(tmp_path, monkeypatch)
    _cli(tmp_path, monkeypatch, _QuotaStore(quota_events(SUBJECT_ID), tmp_path))
    assert m.main(["log", SUBJECT_ID, "-s", "--no-pager"]) == 0
    cold = capsys.readouterr()
    (entry,) = cache_dir().glob("*.json")
    if damage == "corrupt":
        entry.write_bytes(b'{"key": [')
    else:
        shutil.rmtree(cache_dir())
    assert m.main(["log", SUBJECT_ID, "-s", "--no-pager"]) == 0
    rebuilt = capsys.readouterr()
    assert (rebuilt.out, rebuilt.err) == (cold.out, cold.err)
    (entry,) = cache_dir().glob("*.json")
    assert json.loads(entry.read_bytes())["key"][0] == quota_cache.EXTRACT_VERSION

"""The run ledger folded into `imp show <id>`'s card."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from imp.cli.accounting import RowUsage
from imp.cli.main import main
from imp.cli.quota_column import QuotaColumn, QuotaFallback
from imp.cli.quota_share import QuotaFigure, QuotaWithheld, Units, Withheld
from imp.cli.render_summary import (
    StepRow,
    SummaryHeader,
    total_cell,
)
from imp.engine.quest import LAYOUT_VERSION
from imp.paths import project_state_dir

_HDR = SummaryHeader(title="feature", quest_id="abc", status="completed",
                     total_seconds=2342.0)

_NO_READINGS = "quota unavailable; this run recorded no quota readings"


def _row(step: str, secs: int, usage: RowUsage, units: Units) -> StepRow:
    return StepRow(phase="plan", step=step, start_ts="2026-01-01T00:00:00+00:00",
                   end_ts=f"2026-01-01T00:{secs // 60:02d}:{secs % 60:02d}+00:00",
                   usage=usage, units=units, subagents=[])


def _notional(fresh: int = 1_000_000, out: int = 20_000) -> RowUsage:
    """A subscription row: with no resolved gauge its cost cell reads $3.00*."""
    return RowUsage(fresh_input=fresh, output=out, dollars=3.0, priced=1, notional=1)


def _metered(fresh: int = 200_000, out: int = 9_000) -> RowUsage:
    """An API-key row: no notional usage, so its cost cell carries the ~ marker."""
    return RowUsage(fresh_input=fresh, output=out, dollars=0.73, priced=1)


_RESOLVED = [_row("plan-writer", 90, _notional(), Units.of("claude", 3.0))]
_MIXED = [_row("plan-writer", 90, _notional(), Units.of("claude", 3.0)),
          _row("coder", 120, _metered(), Units.of("codex", 2.0))]
_METERED = [_row("coder", 90, _metered(), Units.of("claude", 1.0))]


@pytest.mark.parametrize("rows,quota,expected", [
    # A resolved gauge: a share of the weekly window, so the charge says so.
    (_RESOLVED, QuotaColumn((QuotaFigure("claude", 3.0, 3.0, 10.0),)),
     "39m 2s · 1.0M in · 20.0k out · 0.90% weekly*"),
    # Two providers in the total's units: no single window, no ` weekly*`.
    (_MIXED, QuotaColumn((QuotaFigure("claude", 3.0, 9.0, 10.0),
                          QuotaWithheld("codex", Withheld.BELOW_RESOLUTION))),
     "39m 2s · 1.2M in · 29.0k out · —"),
    # No quota view at all: the metered dollar cell, ~ marker and all.
    (_METERED, None, "39m 2s · 200.0k in · 9.0k out · ~$0.73"),
    # A withheld gauge: the notional dollar cell, * marker and all.
    (_RESOLVED, QuotaFallback((_NO_READINGS,)),
     "39m 2s · 1.0M in · 20.0k out · $3.00*"),
], ids=["resolved", "mixed", "metered", "withheld"])
def test_the_run_charge_is_the_ledger_cell_plus_weekly_only_when_resolved(
    rows: list[StepRow],
    quota: QuotaColumn | QuotaFallback | None,
    expected: str,
) -> None:
    """Spec AC 4: four views, four charges — the ledger's own total cell verbatim,
    with ` weekly*` appended only for a resolved gauge."""
    from imp.cli.main import _run_field

    got = _run_field(rows, _HDR, quota)
    assert got == expected
    charge = got.split(" · ")[-1]
    assert charge.removesuffix(" weekly*") == total_cell(rows, quota)


_USAGE_EVENTS: list[dict[str, object]] = [
    {"type": "user_message", "timestamp": "2026-01-01T00:00:00+00:00",
     "agent_id": None,
     "payload": {"role": "user", "subtype": "intent",
                 "content": [{"type": "text", "text": "build a widget please"}]}},
    {"type": "step_started", "timestamp": "2026-01-01T00:00:00+00:00",
     "agent_id": None, "payload": {"step": "spec-writer", "phase": "spec"}},
    {"type": "agent_message", "timestamp": "2026-01-01T00:00:30+00:00",
     "agent_id": "a1",
     "payload": {"provider": "codex", "model": "gpt-5-codex",
                 "auth_mode": "subscription",
                 "usage": {"input_tokens": 120_000, "cache_read_input_tokens": 30_000,
                           "output_tokens": 4_000}}},
    {"type": "step_completed", "timestamp": "2026-01-01T00:01:30+00:00",
     "agent_id": None,
     "payload": {"step": "spec-writer", "action": {"verb": "continue"}}},
    {"type": "step_started", "timestamp": "2026-01-01T00:01:35+00:00",
     "agent_id": None, "payload": {"step": "plan-writer", "phase": "plan"}},
    {"type": "agent_message", "timestamp": "2026-01-01T00:02:00+00:00",
     "agent_id": "a2",
     "payload": {"provider": "codex", "model": "gpt-5-codex",
                 "auth_mode": "subscription",
                 "usage": {"input_tokens": 60_000, "output_tokens": 2_000}}},
    {"type": "step_completed", "timestamp": "2026-01-01T00:04:45+00:00",
     "agent_id": None,
     "payload": {"step": "plan-writer", "action": {"verb": "continue"}}},
]

_NO_STEP_EVENTS: list[dict[str, object]] = [_USAGE_EVENTS[0]]
_STEP_ONLY_EVENTS: list[dict[str, object]] = _USAGE_EVENTS[1:]
_IDLE_GATE_EVENTS: list[dict[str, object]] = [
    *_USAGE_EVENTS[:4],
    {"type": "step_started", "timestamp": "2026-01-01T00:01:35+00:00",
     "agent_id": None, "payload": {"step": "plan-gate", "phase": "plan"}},
    {"type": "step_completed", "timestamp": "2026-01-01T00:01:35+00:00",
     "agent_id": None,
     "payload": {"step": "plan-gate", "action": {"verb": "continue"}}},
    *_USAGE_EVENTS[4:],
]


def _quest(control: Path, slug: str, *, events: list[dict[str, object]],
           workflow: str | None = None, summary: str | None = None) -> str:
    """A live quest home whose event log is exactly `events`."""
    from imp.engine.state import State

    quest_dir = project_state_dir(control) / "quests" / slug
    (quest_dir / "state").mkdir(parents=True)
    (quest_dir / "quest.json").write_text(
        json.dumps({"id": slug, "title": "Ledger fixture", "base_branch": "main",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "workflow": workflow,
                    "layout_version": LAYOUT_VERSION}))
    (quest_dir / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events))
    State(id=slug, status="paused", step="plan-writer",
          step_attempt=0).save(quest_dir / "state")
    if summary is not None:
        art = quest_dir / "artifacts" / "summary"
        art.mkdir(parents=True)
        (art / f"{slug}.md").write_text(summary)
    return slug


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
           events: list[dict[str, object]], *, slug: str = "ledgerq",
           workflow: str | None = None, summary: str | None = None) -> str:
    import imp.cli.main as m

    _quest(tmp_path, slug, events=events, workflow=workflow, summary=summary)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")
    return slug


def _out(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return capsys.readouterr().out.splitlines()


def test_the_run_field_restates_the_ledger_total_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Spec AC 1 + AC 2: the `run:` line sits between `last activity:` and
    `workflow:`, its four segments are cross-checked against `imp log -s`'s
    own total row, and the fixture's expected value is additionally pinned."""
    slug = _setup(tmp_path, monkeypatch, _IDLE_GATE_EVENTS)
    assert main(["show", slug, "--no-pager"]) == 0
    card = _out(capsys)
    assert main(["log", "-s", slug, "--no-pager"]) == 0
    total = next(ln for ln in _out(capsys) if ln.startswith("  total"))

    labels = [ln.split(":")[0] for ln in card if ln[:1].isalpha() and ":" in ln]
    assert labels[labels.index("last activity") + 1] == "run"
    assert labels[labels.index("run") + 1] == "workflow"
    assert labels[labels.index("workflow") + 1] == "base_branch"

    run = next(ln for ln in card if ln.startswith("run:")).split(":", 1)[1].strip()
    assert run == "4m 45s · 210.0k in · 6.0k out · $0.29*"
    work, tokens_in, tokens_out, charge = run.split(" · ")
    cells = total.split()
    assert cells[-1] == charge
    assert cells[-2] == tokens_out.removesuffix(" out")
    assert cells[-3] == tokens_in.removesuffix(" in")
    assert total.split("total", 1)[1].strip().startswith(work)


@pytest.mark.parametrize("color", [False, True], ids=["plain", "color"])
def test_the_ledger_section_is_the_log_summary_minus_its_header_and_idle_rows(
    color: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The section is `imp log -s`'s own output with the run header line and the
    idle rows dropped — every surviving line byte-identical (widths, hues), under
    color on and off alike; and it is last on the card, after `summary:`."""
    slug = _setup(tmp_path, monkeypatch, _IDLE_GATE_EVENTS,
                  summary="Shipped the widget.\n")
    if color:
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")

    assert main(["show", slug, "--no-pager"]) == 0
    card = _out(capsys)
    assert main(["log", "-s", slug, "--no-pager"]) == 0
    log = _out(capsys)

    from imp.cli import palette as pal
    header = pal.ansi("ledger:", "dim", enabled=color)
    assert card.count(header) == 1
    idx = card.index(header)
    assert card[idx - 1] == ""
    gate = [ln for ln in log if "plan-gate" in ln]
    assert len(gate) == 1
    assert card[idx:] == [header, *[ln for ln in log[1:] if ln not in gate]]
    assert card.index(pal.ansi("summary:", "dim", enabled=color)) < idx


def test_the_ledger_follows_the_field_list_when_there_is_no_prose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Spec AC 3, second placement: an in-flight quest with neither `intent:` nor
    `summary:` — the common `imp show` target — puts the section straight after the
    fields, one blank line down."""
    slug = _setup(tmp_path, monkeypatch, _STEP_ONLY_EVENTS)
    assert main(["show", slug, "--no-pager"]) == 0
    card = _out(capsys)
    assert "intent:" not in card
    assert "summary:" not in card
    idx = card.index("ledger:")
    assert card[idx - 1] == ""
    assert card[idx - 2].startswith("base_branch:")


def test_a_quest_with_no_step_rows_keeps_the_card_it_had(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Spec AC 6: nothing to report, nothing added."""
    slug = _setup(tmp_path, monkeypatch, _NO_STEP_EVENTS)
    assert main(["show", slug, "--no-pager"]) == 0
    card = _out(capsys)
    assert not any(ln.startswith(("run:", "workflow:", "ledger:")) for ln in card)
    assert any(ln.startswith("id:") for ln in card)
    assert "build a widget please" in card          # the intent section still renders


def test_the_card_names_the_quests_own_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Spec AC 8, card half: the quest's own name when it has one, the project
    default when `quest.json` says null — which `_seed_control` pins to `imp`."""
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    memq = _setup(tmp_path, monkeypatch, _USAGE_EVENTS, slug="memq",
                  workflow="memory")
    noneq = _quest(tmp_path, "noneq", events=_USAGE_EVENTS)

    assert main(["show", memq, "--no-pager"]) == 0
    assert "workflow:      memory" in _out(capsys)
    assert main(["show", noneq, "--no-pager"]) == 0
    assert "workflow:      imp" in _out(capsys)


def test_an_archived_record_without_a_workflow_key_reads_as_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Spec AC 8, third shape: a record predating the quest_meta blob has no
    `workflow` key at all, and its ledger still renders off the archive blob."""
    import imp.cli.main as m
    from tests.conftest import _archive_with_event, _seed_control

    _seed_control(tmp_path)
    _archive_with_event(tmp_path, "archq", "Archived",
                        "".join(json.dumps(e) + "\n" for e in _USAGE_EVENTS))
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")

    assert main(["show", "archq", "--no-pager"]) == 0
    card = _out(capsys)
    assert "workflow:      imp" in card
    assert "ledger:" in card
    assert any(ln.startswith("  total") for ln in card)


def test_a_backlog_quest_card_gains_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Spec AC 7: an unstarted backlog quest has no run to report, and its facet
    errors are untouched."""
    import imp.cli.main as m
    from imp.engine.quest import Quest
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    Quest.create_draft(tmp_path, title="Queued", quest_id="bklg01",
                       base_branch="main", origin="followup",
                       seed_opening="Queued\n\ndo it later")
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")

    assert main(["show", "bklg01", "--no-pager"]) == 0
    card = _out(capsys)
    assert any(ln.startswith("status:") and ln.endswith("backlog") for ln in card)
    assert not any(ln.startswith(("run:", "workflow:", "ledger:")) for ln in card)
    assert main(["show", "bklg01", "spec"]) == 2
    assert ("bklg01 is in the backlog (not started); no spec yet"
            in capsys.readouterr().err)


def test_show_json_gains_no_ledger_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Spec AC 13: the machine shape is unchanged — no schema pinned for the
    cost-versus-quota split until something asks for one."""
    slug = _setup(tmp_path, monkeypatch, _USAGE_EVENTS)
    assert main(["show", slug, "--json", "--no-pager"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {"run", "workflow", "ledger"}.isdisjoint(data)
    assert data["id"] == slug


def test_a_quota_attribution_failure_prints_no_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Spec AC 12: an under-counted denominator must never pass as a figure, and a
    card without its ledger would need a second rendering mode — so `imp show`
    fails exactly as `imp log -s` does."""
    import imp.cli.store as s

    class _Broken(s.LocalStore):
        def quota_neighbours(self, quest_id: str, start_iso: str,
                             end_iso: str) -> list[s.NeighbourQuota]:
            raise OSError("neighbour scan exploded")

    slug = _setup(tmp_path, monkeypatch, _USAGE_EVENTS)
    monkeypatch.setattr(s, "make_store", lambda control: _Broken(control))

    assert main(["show", slug, "--no-pager"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert ("cannot reach the quest store for quota attribution "
            "(quota_neighbours): neighbour scan exploded") in captured.err


def _at(seconds: int) -> str:
    return (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=seconds)).isoformat()


def _visit(start: int, secs: int, step: str, phase: str, label: str | None,
           phase_label: str | None, *inner: dict[str, object],
           ) -> list[dict[str, object]]:
    payload: dict[str, object] = {"step": step, "phase": phase, "label": label}
    if phase_label is not None:
        payload["phase_label"] = phase_label
    return [
        {"type": "step_started", "timestamp": _at(start), "agent_id": None,
         "payload": payload},
        *inner,
        {"type": "step_completed", "timestamp": _at(start + secs), "agent_id": None,
         "payload": {"step": step, "action": {"verb": "continue"}}},
    ]


_SCOPING = "scoping phase"
_SPEC = "spec phase"
_PLAN = "planning phase · plan 1/3"
_IMPLEMENT = "implementation phase · plan 1/3"

_LABELED_EVENTS: list[dict[str, object]] = [
    *_visit(0, 354, "threshold", "threshold", "chatting", _SCOPING,
            {"type": "tool_call", "id": "ev1", "timestamp": _at(60), "agent_id": "a1",
             "payload": {"name": "sub_agent", "id": "b1", "id_ns": "block",
                         "label": "exploring codebase",
                         "input": {"subagent_name": "explorer"}}},
            {"type": "tool_result", "timestamp": _at(157), "agent_id": "a1",
             "payload": {"id": "b1", "id_ns": "block"}}),
    *_visit(354, 0, "scope-code-check", "threshold", "checking for stray commits", _SCOPING),
    *_visit(354, 53, "spec-finalizer", "spec", "writing spec", _SPEC),
    *_visit(407, 0, "spec-verify-commit", "spec", "checking spec commit", _SPEC),
    *_visit(407, 331, "spec-reviewer", "spec", "reviewing spec", _SPEC),
    *_visit(738, 113, "spec-finalizer", "spec", "addressing spec review feedback", _SPEC),
    *_visit(851, 1550, "plan-writer", "plan", "writing plan", _PLAN),
    *_visit(2401, 1091, "task-lead", "implement", "task 1/2", _IMPLEMENT,
            {"type": "step_label_updated", "timestamp": _at(2700), "agent_id": "a2",
             "payload": {"label": "task 2/2"}}),
    *_visit(3492, 0, "verify", "implement", "running tests", _IMPLEMENT),
    *_visit(3492, 38, "task-lead", "implement", "fixing test failures", _IMPLEMENT),
]

_LABELED_LEDGER = [
    "                                        time  in  out  cost",
    "scoping phase",
    "  chatting                            5m 54s   —    —     —",
    "    ↳ explorer (exploring codebase)   1m 37s   —    —     —",
    "  checking for stray commits              0s   —    —     —",
    "spec phase",
    "  writing spec                           53s   —    —     —",
    "  checking spec commit                    0s   —    —     —",
    "  reviewing spec                      5m 31s   —    —     —",
    "  addressing spec review feedback     1m 53s   —    —     —",
    "planning phase · plan 1/3",
    "  writing plan                       25m 50s   —    —     —",
    "implementation phase · plan 1/3",
    "  task 2/2                           18m 11s   —    —     —",
    "  running tests                           0s   —    —     —",
    "  fixing test failures                   38s   —    —     —",
    "",
    "  total                              58m 50s   —    —     —",
]

_LABELED_CARD_LEDGER = [
    "                                        time  in  out  cost",
    "scoping phase",
    "  chatting                            5m 54s   —    —     —",
    "    ↳ explorer (exploring codebase)   1m 37s   —    —     —",
    "spec phase",
    "  writing spec                           53s   —    —     —",
    "  reviewing spec                      5m 31s   —    —     —",
    "  addressing spec review feedback     1m 53s   —    —     —",
    "planning phase · plan 1/3",
    "  writing plan                       25m 50s   —    —     —",
    "implementation phase · plan 1/3",
    "  task 2/2                           18m 11s   —    —     —",
    "  fixing test failures                   38s   —    —     —",
    "",
    "  total                              58m 50s   —    —     —",
]

_PRE_UPGRADE_EVENTS: list[dict[str, object]] = [
    *_visit(0, 60, "threshold", "threshold", None, None),
    *_visit(60, 0, "scope-code-check", "threshold", None, None),
]

_RESUMED_CONVERSATION_EVENTS: list[dict[str, object]] = [
    {"type": "step_started", "timestamp": _at(0), "agent_id": None,
     "payload": {"step": "threshold", "phase": "threshold", "label": None}},
    *_visit(30, 30, "threshold", "threshold", "chatting", _SCOPING),
]


def test_show_and_log_summary_render_the_labeled_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    slug = _setup(tmp_path, monkeypatch, _LABELED_EVENTS)
    assert main(["log", "-s", slug, "--no-pager"]) == 0
    log = _out(capsys)
    assert main(["show", slug, "--no-pager"]) == 0
    card = _out(capsys)
    assert log[1:1 + len(_LABELED_LEDGER)] == _LABELED_LEDGER
    start = card.index("ledger:") + 1
    assert card[start:start + len(_LABELED_CARD_LEDGER)] == _LABELED_CARD_LEDGER


def test_a_pre_upgrade_log_shows_raw_ids_under_the_phase_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    slug = _setup(tmp_path, monkeypatch, _PRE_UPGRADE_EVENTS)
    assert main(["log", "-s", slug, "--no-pager"]) == 0
    assert _out(capsys)[1:5] == [
        "                     time  in  out  cost",
        "threshold phase",
        "  threshold         1m 0s   —    —     —",
        "  scope-code-check     0s   —    —     —",
    ]


def test_a_conversation_resumed_after_the_upgrade_stays_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    slug = _setup(tmp_path, monkeypatch, _RESUMED_CONVERSATION_EVENTS)
    assert main(["log", "-s", slug, "--no-pager"]) == 0
    assert _out(capsys)[2:4] == [
        "threshold phase",
        "  chatting  1m 0s   —    —     —",
    ]


def _export(slug: str, flag: str, capsys: pytest.CaptureFixture[str]) -> str:
    assert main(["log", "-s", slug, flag]) == 0
    return capsys.readouterr().out


def test_summary_markdown_carries_the_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _export(_setup(tmp_path, monkeypatch, _LABELED_EVENTS), "--md=-", capsys)
    headings = [ln for ln in out.splitlines() if ln.startswith("## ")]
    assert headings == ["## scoping phase", "## spec phase",
                        "## planning phase · plan 1/3",
                        "## implementation phase · plan 1/3"]
    for cell in ("| chatting | 5m 54s |",
                 "| ↳ explorer (exploring codebase) | 1m 37s |",
                 "| checking for stray commits | 0s |",
                 "| addressing spec review feedback | 1m 53s |",
                 "| task 2/2 | 18m 11s |",
                 "| fixing test failures | 38s |"):
        assert cell in out
    for raw in ("| threshold |", "| spec-finalizer |", "| task-lead |", "| task 1/2 |"):
        assert raw not in out


def test_summary_html_carries_the_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _export(_setup(tmp_path, monkeypatch, _LABELED_EVENTS), "--html=-", capsys)
    for fragment in ("<h2>scoping phase</h2>", "<h2>planning phase · plan 1/3</h2>",
                     "<td>chatting</td>", "<td>checking for stray commits</td>",
                     "<td>addressing spec review feedback</td>", "<td>task 2/2</td>"):
        assert fragment in out
    for raw in ("<td>threshold</td>", "<td>spec-finalizer</td>", "<td>task-lead</td>"):
        assert raw not in out


def test_summary_markdown_of_a_pre_upgrade_log_shows_raw_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _export(_setup(tmp_path, monkeypatch, _PRE_UPGRADE_EVENTS), "--md=-", capsys)
    assert "## threshold phase\n" in out
    assert "| threshold | 1m 0s |" in out
    assert "| scope-code-check | 0s |" in out


def test_summary_html_of_a_pre_upgrade_log_shows_raw_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _export(_setup(tmp_path, monkeypatch, _PRE_UPGRADE_EVENTS), "--html=-", capsys)
    assert "<h2>threshold phase</h2>" in out
    assert "<td>threshold</td>" in out
    assert "<td>scope-code-check</td>" in out


def test_summary_export_of_a_resumed_conversation_stays_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _export(_setup(tmp_path, monkeypatch, _RESUMED_CONVERSATION_EVENTS),
                  "--md=-", capsys)
    assert "## threshold phase\n" in out
    assert "| chatting | 1m 0s |" in out

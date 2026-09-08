"""The live footer's inputs reach the row from the streamed events (spec §4)."""

from __future__ import annotations

import io
from typing import Any

import pytest

from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.cli.render_summary import build_summary, summary_totals
from imp.providers.quota import QuotaReading, QuotaWindow
from tests._event_fixtures import ev

_STEP = "step:implement phase/coder"


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def _drive(events: list[dict[str, Any]]) -> LiveDriver:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=200, status=LiveStatus(out))
    for e in events:
        drv.on_event(e)
    return drv


def _position_row(events: list[dict[str, Any]]) -> str:
    rows = _drive(events).compose_status(0)
    assert rows is not None
    return rows[2]


def _reading(provider: str, *windows: QuotaWindow) -> QuotaReading:
    return QuotaReading(provider=provider, plan=None, windows=windows, raw={})


def _cc_events(*tail: dict[str, Any],
               auth_mode: str = "apikey") -> list[dict[str, Any]]:
    """A claude_code step: its agent_message usage is never priced, so any dollars
    on this row can only have come from a query_result."""
    return [
        ev("1", "quest_created", quest_id="pazo49", title="t"),
        ev("2", "step_started", step="coder", kind="agent",
           phase="implement phase", model="claude-opus-5", provider="claude_code"),
        ev("3", "agent_message", agent_id=_STEP, provider="claude_code",
           model="claude-opus-5", auth_mode=auth_mode, stop_reason="end_turn",
           raw={"content": [{"type": "text", "text": "done"}]},
           usage={"input_tokens": 120000, "output_tokens": 9000}),
        *tail,
    ]


def _query_result(eid: str, session_id: str, dollars: float,
                  out_tokens: int) -> dict[str, Any]:
    return ev(eid, "query_result", agent_id=_STEP, session_id=session_id,
              model_usage={"claude-opus-5": {"inputTokens": 120000,
                                             "outputTokens": out_tokens}},
              total_cost_usd=dollars)


def _codex_events(*tail: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        ev("1", "quest_created", quest_id="pazo49", title="t"),
        ev("2", "step_started", step="coder", kind="agent",
           phase="implement phase", model="gpt-5.6-terra", provider="codex"),
        ev("3", "agent_message", agent_id=_STEP, provider="codex",
           model="gpt-5.6-terra", auth_mode="apikey", stop_reason="end_turn",
           raw={"content": [{"type": "text", "text": "done"}]}, usage={}),
        *tail,
    ]


def _subagent_events(*, auth_mode: str | None,
                     usage: dict[str, Any]) -> list[dict[str, Any]]:
    """A codex step that prices its own turn and then dispatches a subagent. The
    parent's turn is priced before the dispatch, so the row can carry a figure
    without a later parent-agent_id event resume-closing the open fold."""
    parent = ev("3", "agent_message", agent_id=_STEP, provider="codex",
                model="gpt-5.6-terra", stop_reason="end_turn",
                raw={"content": [{"type": "text", "text": "dispatching"}]},
                usage=usage)
    if auth_mode is not None:
        parent["payload"]["auth_mode"] = auth_mode
    reading = _reading("codex", QuotaWindow("5h", 12.0, None))
    return [
        ev("1", "quest_created", quest_id="pazo49", title="t"),
        ev("2", "step_started", step="coder", kind="agent",
           phase="implement phase", model="gpt-5.6-terra", provider="codex"),
        parent,
        ev("q1", "quota_reading", agent_id=_STEP, **reading.to_payload()),
        ev("t1", "tool_call", agent_id=_STEP, id="blk-t1", name="sub_agent",
           input={"subagent_name": "coder"}),
        ev("c1", "step_context", agent_id="sub:t1", provider="codex",
           model="gpt-5.6-terra", tools_schema=[], params={},
           max_tool_calls=None),
        ev("r1", "agent_message", agent_id="sub:t1", provider="codex",
           model="gpt-5.6-terra", stop_reason="end_turn",
           raw={"content": [{"type": "text", "text": "hi"}]},
           usage={"input_tokens": 60000, "output_tokens": 5}, context_pct=42.0),
    ]


def _usage_patch(fresh: int, out: int, context_tokens: int) -> dict[str, Any]:
    # Persisted and broadcast without an id or a timestamp (state.py) — codex's
    # only source of dollars, and unusable as a dedupe key.
    return {"type": "agent_message_usage_patch", "agent_id": _STEP,
            "payload": {"usage": {"input_tokens": fresh, "output_tokens": out},
                        "context_tokens": context_tokens}}


# AC 15
def test_the_cost_field_reaches_the_row_from_the_streamed_events() -> None:
    events = _cc_events(_query_result("r1", "sess-1", 4.21, 9000))
    assert _position_row(events).endswith(" · $4.21")


# AC 15
def test_the_quota_field_reaches_the_row_from_the_streamed_events() -> None:
    reading = _reading("claude_code", QuotaWindow("5h", 34.0, None),
                       QuotaWindow("7d", 7.0, None))
    events = _cc_events(_query_result("r1", "sess-1", 4.21, 9000),
                        ev("q1", "quota_reading", agent_id=_STEP,
                           **reading.to_payload()),
                        auth_mode="subscription")
    row = _position_row(events)
    # The same priced sequence: the auth mode alone decides which field shows.
    assert row.endswith(" · session 34% · week 7%")
    assert "$" not in row


# AC 16(a)
def test_the_live_figure_is_the_vendor_total_the_summary_reports() -> None:
    events = _cc_events(_query_result("r1", "sess-1", 4.21, 9000))
    total = summary_totals(build_summary(events).rows, None)[1]
    # Not $0.00, and not a price-table figure derived from the agent_message.
    assert total.dollars == pytest.approx(4.21)
    assert _position_row(events).endswith(f" · ${total.dollars:.2f}")


# AC 11
def test_the_cost_field_prices_a_query_result_carrying_no_vendor_cost() -> None:
    # The footer derives from the same RowUsage, so a fallback-priced row
    # reaches it bare, with no sigil: 5,000*$3 + 200,000*$0.30 + 8,000*$15 per
    # million = $0.195 on claude-sonnet-4-6.
    events = _cc_events(ev("r1", "query_result", agent_id=_STEP,
                           session_id="sess-1",
                           model_usage={"claude-sonnet-4-6": {
                               "inputTokens": 5000, "outputTokens": 8000,
                               "cacheReadInputTokens": 200000}}))
    assert _position_row(events).endswith(" · $0.20")


# AC 16(b)
def test_the_live_figure_sums_usage_patches_that_carry_no_id() -> None:
    events = _codex_events(_usage_patch(100000, 8000, 100000),
                           _usage_patch(50000, 4000, 150000))
    total = summary_totals(build_summary(events).rows, None)[1]
    assert total.priced == 2 and total.dollars > 0     # both rows counted, once each
    assert _position_row(events).endswith(f" · ${total.dollars:.2f}")


# AC 16(c)
def test_the_live_figure_collapses_cumulative_rows_per_session() -> None:
    events = _cc_events(_query_result("r1", "sess-1", 4.21, 9000),
                        _query_result("r2", "sess-1", 5.50, 11000),
                        _query_result("r3", "sess-2", 0.75, 1000))
    total = summary_totals(build_summary(events).rows, None)[1]
    # sess-1 is cumulative — only its last row counts — and sess-2 adds.
    assert total.dollars == pytest.approx(6.25)
    assert _position_row(events).endswith(f" · ${total.dollars:.2f}")


# AC 16(d)
def test_a_contribution_for_an_unrelated_agent_leaves_the_field_absent() -> None:
    events = _cc_events(ev("r1", "query_result", agent_id="step:other/agent",
                           session_id="sess-9", model_usage={},
                           total_cost_usd=9.99))
    total = summary_totals(build_summary(events).rows, None)[1]
    assert total.priced == 0                  # attributed to nothing, priced as nothing
    row = _position_row(events)
    assert "$" not in row and "n/a" not in row
    assert row.endswith("claude-opus-5 · ██████░░░░ 60%")


# AC 14
def test_a_mixed_provider_run_renders_the_active_steps_carrier() -> None:
    codex_reading = _reading("codex", QuotaWindow("5h", 12.0, None))
    started = [
        ev("1", "quest_created", quest_id="pazo49", title="t"),
        ev("2", "step_started", step="a", kind="agent", phase="p1",
           model="gpt-5.6-terra", provider="codex"),
        ev("q1", "quota_reading", agent_id="step:p1/a",
           **codex_reading.to_payload()),
    ]
    assert _position_row(started).endswith(" · session 12%")

    crossed = [
        *started,
        ev("3", "step_completed", step="a", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
        ev("4", "step_started", step="b", kind="agent", phase="p2",
           model="claude-opus-5", provider="claude_code"),
    ]
    # codex's level says nothing about the claude account: no field, no placeholder.
    assert _position_row(crossed).endswith("claude-opus-5")


# AC 12 (subscription half)
def test_a_subagent_row_carries_the_gauge_and_never_the_quota() -> None:
    rows = _drive(_subagent_events(auth_mode=None, usage={})).compose_status(0)
    assert rows is not None and len(rows) == 4
    assert rows[2].endswith(" · session 12%")          # position row: quota
    assert "████░░░░░░ 42%" in rows[3]                 # subagent row: same gauge
    assert "session" not in rows[3] and "week" not in rows[3]   # never the account level


# AC 12 (metered half)
def test_a_subagent_row_carries_no_cost_field_on_a_metered_backend() -> None:
    rows = _drive(_subagent_events(
        auth_mode="apikey",
        usage={"input_tokens": 100000, "output_tokens": 8000})).compose_status(0)
    assert rows is not None and len(rows) == 4
    # The position row takes the metered branch; the recorded reading stays off it.
    assert "$" in rows[2] and "session" not in rows[2]
    assert "████░░░░░░ 42%" in rows[3]                 # same gauge, same grammar
    assert "session" not in rows[3] and "$" not in rows[3]

"""The live footer's inputs reach the row from the streamed events (spec §4)."""

from __future__ import annotations

import io
from typing import Any

import pytest

from imp.cli import palette as pal
from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.cli.render_summary import build_summary, summary_totals
from imp.providers.quota import QuotaReading, QuotaWindow
from tests._event_fixtures import ev

_STEP = "step:implement phase/coder"
_WINDOW = 258_400


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


@pytest.fixture(autouse=True)
def _wide_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """compose_status takes its width from term_width(), not the driver's `width`.
    Pin it wide so the fit ladder admits the right group instead of dropping it at
    CI's ~80 columns."""
    monkeypatch.setenv("COLUMNS", "200")


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
    """A codex step that records its turn and then dispatches a subagent."""
    parent = ev("3", "agent_message", agent_id=_STEP, provider="codex",
                model="gpt-5.6-terra", stop_reason="end_turn",
                raw={"content": [{"type": "text", "text": "dispatching"}]},
                usage={})
    if auth_mode is not None:
        parent["payload"]["auth_mode"] = auth_mode
    reading = _reading(
        "codex",
        QuotaWindow("5h", 12.0, None),
        QuotaWindow("7d", 7.0, None),
    )
    return [
        ev("1", "quest_created", quest_id="pazo49", title="t"),
        ev("2", "step_started", step="coder", kind="agent",
           phase="implement phase", model="gpt-5.6-terra", provider="codex"),
        parent,
        ev("q1", "quota_reading", agent_id=_STEP, **reading.to_payload()),
        _usage_patch(
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
            22_389,
            _WINDOW,
        ),
        ev("t1", "tool_call", agent_id=_STEP, id="blk-t1", name="sub_agent",
           input={"subagent_name": "coder"}),
        ev("c1", "step_context", agent_id="sub:t1", provider="codex",
           model="gpt-5.6-terra", tools_schema=[], params={},
           max_tool_calls=None),
        ev("r1", "agent_message", agent_id="sub:t1", provider="codex",
           model="gpt-5.6-terra", stop_reason="end_turn",
           raw={"content": [{"type": "text", "text": "hi"}]}, usage={}),
        _usage_patch(
            60_000,
            5,
            108_528,
            _WINDOW,
            agent_id="sub:t1",
        ),
    ]


def _usage_patch(
    fresh: int,
    out: int,
    context_tokens: int,
    context_window: int,
    *,
    agent_id: str = _STEP,
) -> dict[str, Any]:
    # Persisted and broadcast without an id or a timestamp (state.py) — codex's
    # only source of dollars, and unusable as a dedupe key.
    return {
        "type": "agent_message_usage_patch",
        "agent_id": agent_id,
        "payload": {
            "usage": {"input_tokens": fresh, "output_tokens": out},
            "context_tokens": context_tokens,
            "context_window": context_window,
        },
    }


# AC 15
def test_the_cost_field_reaches_the_row_from_the_streamed_events() -> None:
    events = _cc_events(_query_result("r1", "sess-1", 4.21, 9000))
    row = _position_row(events)
    # 34 + 159 + 5 = 198 = width - 2: the dollars sit at the band's right edge.
    assert row == "t · claude-opus-5 · ██████░░░░ 60%" + " " * 159 + "$4.21"
    assert pal.display_width(row) == 198


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
    assert row == ("t · claude-opus-5 · ██████░░░░ 60%" + " " * 129
                   + "claude_code · session 34% · week 7%")
    assert "$" not in row


# AC 16(a)
def test_the_live_figure_is_the_vendor_total_the_summary_reports() -> None:
    events = _cc_events(_query_result("r1", "sess-1", 4.21, 9000))
    total = summary_totals(build_summary(events), None)[1]
    # Not $0.00, and not a price-table figure derived from the agent_message.
    assert total.dollars == pytest.approx(4.21)
    assert _position_row(events).endswith(f"${total.dollars:.2f}")


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
    assert _position_row(events).endswith("$0.20")


# AC 16(b)
def test_the_live_figure_sums_usage_patches_that_carry_no_id() -> None:
    events = _codex_events(
        _usage_patch(100000, 8000, 100000, _WINDOW),
        _usage_patch(50000, 4000, 150000, _WINDOW),
    )
    total = summary_totals(build_summary(events), None)[1]
    assert total.priced == 2 and total.dollars > 0     # both rows counted, once each
    assert _position_row(events).endswith(f"${total.dollars:.2f}")


# AC 16(c)
def test_the_live_figure_collapses_cumulative_rows_per_session() -> None:
    events = _cc_events(_query_result("r1", "sess-1", 4.21, 9000),
                        _query_result("r2", "sess-1", 5.50, 11000),
                        _query_result("r3", "sess-2", 0.75, 1000))
    total = summary_totals(build_summary(events), None)[1]
    # sess-1 is cumulative — only its last row counts — and sess-2 adds.
    assert total.dollars == pytest.approx(6.25)
    assert _position_row(events).endswith(f"${total.dollars:.2f}")


# AC 16(d)
def test_a_contribution_for_an_unrelated_agent_leaves_the_field_absent() -> None:
    events = _cc_events(ev("r1", "query_result", agent_id="step:other/agent",
                           session_id="sess-9", model_usage={},
                           total_cost_usd=9.99))
    total = summary_totals(build_summary(events), None)[1]
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
    assert _position_row(started).endswith("codex · session 12%")

    crossed = [
        *started,
        ev("3", "step_completed", step="a", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
        ev("4", "step_started", step="b", kind="agent", phase="p2",
           model="claude-opus-5", provider="claude_code"),
    ]
    # codex's level says nothing about the claude account: no field, no placeholder.
    assert _position_row(crossed).endswith("claude-opus-5")


# AC 2(b-c), 3(a-c), 6(i)
def test_codex_usage_patches_drive_parent_and_subagent_footer_rows() -> None:
    rows = _drive(_subagent_events(
        auth_mode="chatgpt",
        usage={"input_tokens": 82_628, "output_tokens": 328},
    )).compose_status(0)

    assert rows is not None and len(rows) == 4
    expected_parent = (
        "t · gpt-5.6-terra · █░░░░░░░░░ 9%"
        + " " * 136
        + "codex · session 12% · week 7%"
    )
    assert rows[2] == expected_parent
    assert pal.display_width(rows[2]) == 198
    assert "$" not in rows[2]
    assert "████░░░░░░ 42%" in rows[3]
    assert "session" not in rows[3]
    assert "week" not in rows[3]
    assert "$" not in rows[3]


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

"""The live footer's inputs reach the row from the streamed events (spec §4)."""

from __future__ import annotations

import io
import time
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
    """A claude step: its agent_message usage is never priced, so any dollars
    on this row can only have come from a query_result."""
    return [
        ev("1", "quest_created", quest_id="pazo49", title="t"),
        ev("2", "step_started", step="coder", kind="agent",
           phase="implement phase", model="claude-opus-5", provider="claude"),
        ev("3", "agent_message", agent_id=_STEP, provider="claude",
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
           phase="implement phase", model="gpt-6.1-terra", provider="codex"),
        ev("3", "agent_message", agent_id=_STEP, provider="codex",
           model="gpt-6.1-terra", auth_mode="apikey", stop_reason="end_turn",
           raw={"content": [{"type": "text", "text": "done"}]}, usage={}),
        *tail,
    ]


def _subagent_events(*, auth_mode: str | None,
                     usage: dict[str, Any]) -> list[dict[str, Any]]:
    """A codex step that records its turn and then dispatches a subagent."""
    parent = ev("3", "agent_message", agent_id=_STEP, provider="codex",
                model="gpt-6.1-terra", stop_reason="end_turn",
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
           phase="implement phase", model="gpt-6.1-terra", provider="codex"),
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
           model="gpt-6.1-terra", tools_schema=[], params={},
           max_tool_calls=None),
        ev("r1", "agent_message", agent_id="sub:t1", provider="codex",
           model="gpt-6.1-terra", stop_reason="end_turn",
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
    # 40 + 153 + 5 = 198 = width - 2: the dollars sit at the band's right edge.
    assert row == "t · claude-opus-5 · ██████░░░░ 120k/200k" + " " * 153 + "$4.21"
    assert pal.display_width(row) == 198


# AC 15
def test_the_quota_field_reaches_the_row_from_the_streamed_events() -> None:
    reading = _reading("claude", QuotaWindow("5h", 34.0, None),
                       QuotaWindow("7d", 7.0, None))
    events = _cc_events(_query_result("r1", "sess-1", 4.21, 9000),
                        ev("q1", "quota_reading", agent_id=_STEP,
                           **reading.to_payload()),
                        auth_mode="subscription")
    row = _position_row(events)
    # The same priced sequence: the auth mode alone decides which field shows.
    assert row == ("t · claude-opus-5 · ██████░░░░ 120k/200k" + " " * 128
                   + "claude · session 34% · week 7%")
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
    assert row.endswith("claude-opus-5 · ██████░░░░ 120k/200k")


# AC 4, AC 14
def test_a_mixed_provider_run_renders_the_active_steps_carrier() -> None:
    codex_reading = _reading("codex", QuotaWindow("5h", 12.0, None))
    started = [
        ev("1", "quest_created", quest_id="pazo49", title="t"),
        ev("2", "step_started", step="a", kind="agent", phase="p1",
           model="gpt-6.1-terra", provider="codex"),
        # A step-level turn, so codex becomes the last-known provider (and states
        # no auth_mode, so its reading — not dollars — is what the row can show).
        ev("m1", "agent_message", agent_id="step:p1/a", provider="codex",
           model="gpt-6.1-terra", stop_reason="end_turn",
           raw={"content": [{"type": "text", "text": "done"}]}, usage={}),
        ev("q1", "quota_reading", agent_id="step:p1/a",
           **codex_reading.to_payload()),
    ]
    assert _position_row(started).endswith("codex · session 12%")

    crossed = [
        *started,
        ev("3", "step_completed", step="a", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
        ev("4", "step_started", step="b", kind="agent", phase="p2",
           model="claude-opus-5", provider="claude"),
    ]
    # codex's level says nothing about the claude account: no field, no
    # placeholder — the running step's carrier wins over the last-known one.
    row = _position_row(crossed)
    assert row.endswith("claude-opus-5")
    assert "codex" not in row and "session" not in row


# AC 2(b-c), 3(a-c), 6(i)
def test_codex_usage_patches_drive_parent_and_subagent_footer_rows() -> None:
    rows = _drive(_subagent_events(
        auth_mode="chatgpt",
        usage={"input_tokens": 82_628, "output_tokens": 328},
    )).compose_status(0)

    assert rows is not None and len(rows) == 4
    expected_parent = (
        "t · gpt-6.1-terra · █░░░░░░░░░ 22k/200k"
        + " " * 130
        + "codex · session 12% · week 7%"
    )
    assert rows[2] == expected_parent
    assert pal.display_width(rows[2]) == 198
    assert "$" not in rows[2]
    assert "█████░░░░░ 109k/200k" in rows[3]
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
    assert "█████░░░░░ 109k/200k" in rows[3]                 # same gauge, same grammar
    assert "session" not in rows[3] and "$" not in rows[3]


def test_compose_status_passes_the_live_width_to_the_activity_line(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """One term_width() per compose, shared by the activity line and the position row:
    at 80 columns the activity line keeps the countdown and sheds the p hint."""
    from imp.cli import live_driver as ld
    drv = _drive([
        ev("1", "quest_created", quest_id="pazo49", title="t"),
        ev("2", "step_started", step="coder", kind="agent", phase="implement phase",
           model="claude-opus-5", provider="anthropic"),
        ev("3", "agent_retry", category="rate_limit", attempt=1,
           backoff_seconds=2940.0, hinted=True),
    ])
    calls: list[int] = []

    def _counting_term_width() -> int:
        calls.append(80)
        return 80

    monkeypatch.setattr(ld, "term_width", _counting_term_width)
    rows = drv.compose_status(0)
    assert rows is not None
    assert len(calls) == 1
    assert "press r to retry now" in rows[0]
    assert "press p to switch provider" not in rows[0]
    assert rows[0].endswith("49m left")
    assert pal.clip_display(rows[0], 79) == rows[0]


def test_compose_startup_passes_the_live_width_to_the_activity_line(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Two term_width() calls: the activity line takes the live width, as the identity
    row already does — a hardcoded width would leave only one."""
    from imp.cli import live_driver as ld
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=200, status=LiveStatus(out),
                     startup_verb="resuming", quest_id="pazo49", title="t")
    calls: list[int] = []

    def _counting_term_width() -> int:
        calls.append(200)
        return 200

    monkeypatch.setattr(ld, "term_width", _counting_term_width)
    rows = drv.compose_startup(0)
    assert rows is not None
    assert "resuming" in rows[0]
    assert len(calls) == 2


_WINDOWS = "session 62% (3h left) · week 7% (5d left)"


def _future_reading(provider: str) -> QuotaReading:
    """Resets 3h and ~5d out, sat mid-bucket: 30 min of margin before the session
    window would coarsen to `2h` and ~11h before the weekly one reads `4d`, so a
    run stalled under load still renders the same countdown."""
    now = int(time.time())
    return _reading(provider, QuotaWindow("5h", 62.0, now + 10_800),
                    QuotaWindow("7d", 7.0, now + 430_000))


def _quota_event(provider: str) -> dict[str, Any]:
    return ev("q1", "quota_reading", agent_id=_STEP,
              **_future_reading(provider).to_payload())


def _chrome_tail() -> list[dict[str, Any]]:
    """The agent step ends and a script step takes the stack top: its Activity
    carries provider=None, which is the hole that used to empty the right group."""
    return [
        ev("8", "step_completed", step="coder", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
        ev("9", "step_started", step="verify", kind="script",
           phase="implement phase", command="pytest -q"),
    ]


def _chrome_row(events: list[dict[str, Any]]) -> str:
    return _position_row([*events, *_chrome_tail()])


# AC 1
def test_the_footer_keeps_the_group_through_a_chrome_step() -> None:
    row = _chrome_row(_cc_events(_quota_event("claude"),
                                 auth_mode="subscription"))
    assert row.endswith("claude · " + _WINDOWS)
    assert pal.display_width(row) == 198       # still anchored to the band's edge
    assert row.startswith("t ")                # the script step contributes no segment


# AC 6
def test_the_chrome_row_carries_no_context_gauge() -> None:
    row = _chrome_row(_cc_events(_quota_event("claude"),
                                 auth_mode="subscription"))
    assert row.endswith(_WINDOWS)
    # The gauge stays gated on the activity's kind, not on the resolved carrier.
    assert "█" not in row and "░" not in row


# AC 2
def test_the_your_turn_prompt_keeps_the_group_after_the_turn_ends() -> None:
    drv = _drive(_cc_events(
        _quota_event("claude"),
        ev("4", "step_completed", step="coder", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
        auth_mode="subscription"))
    rows = drv.compose_status(0)
    assert rows is not None and "working" in rows[0]   # the step stack is drained
    toolbar = drv.compose_toolbar_static()
    assert toolbar is not None
    assert toolbar.endswith("claude · " + _WINDOWS)


# AC 3(a)
def test_an_api_only_provider_keeps_its_dollars_through_a_chrome_step() -> None:
    events = [
        ev("1", "quest_created", quest_id="pazo49", title="t"),
        ev("2", "step_started", step="coder", kind="agent",
           phase="implement phase", model="claude-opus-5", provider="anthropic"),
        ev("3", "agent_message", agent_id=_STEP, provider="anthropic",
           model="claude-opus-5", stop_reason="end_turn",
           raw={"content": [{"type": "text", "text": "done"}]},
           usage={"input_tokens": 120000, "output_tokens": 9000}),
    ]
    total = summary_totals(build_summary(events), None)[1]
    assert total.priced == 1 and total.dollars > 0    # anthropic states no auth_mode
    row = _chrome_row(events)
    assert row.endswith(f" ${total.dollars:.2f}")
    assert "session" not in row and "week" not in row


# AC 3(b)
def test_an_api_keyed_subscription_provider_keeps_its_dollars_too() -> None:
    events = _cc_events(_query_result("r1", "sess-1", 4.21, 9000),
                        _quota_event("claude"), auth_mode="apikey")
    row = _chrome_row(events)
    assert row.endswith(" $4.21")
    assert "session" not in row and "claude" not in row


# AC 5
def test_a_subagent_provider_never_becomes_the_footers_fallback() -> None:
    events = _cc_events(
        _quota_event("claude"),
        ev("t1", "tool_call", agent_id=_STEP, id="blk-t1", name="sub_agent",
           input={"subagent_name": "coder"}),
        ev("c1", "step_context", agent_id="sub:t1", provider="codex",
           model="gpt-6.1-terra", tools_schema=[], params={},
           max_tool_calls=None),
        ev("r1", "agent_message", agent_id="sub:t1", provider="codex",
           model="gpt-6.1-terra", stop_reason="end_turn",
           raw={"content": [{"type": "text", "text": "hi"}]}, usage={}),
        auth_mode="subscription")
    assert _chrome_row(events).endswith("claude · " + _WINDOWS)


# AC 11
def test_a_chrome_step_with_no_reading_and_no_dollars_renders_no_group() -> None:
    # A claude agent_message prices nothing, and no reading was recorded.
    row = _chrome_row(_cc_events(auth_mode="subscription"))
    assert row == "t"
    assert "n/a" not in row and "—" not in row


# AC 12
def test_the_fallback_group_degrades_by_the_same_ladder(
        monkeypatch: pytest.MonkeyPatch) -> None:
    events = _cc_events(_quota_event("claude"), auth_mode="subscription")
    monkeypatch.setenv("COLUMNS", "50")     # 48 columns carry the windows, not the name
    narrow = _chrome_row(events)
    assert narrow.endswith(_WINDOWS) and "claude" not in narrow
    monkeypatch.setenv("COLUMNS", "30")     # neither candidate fits
    assert _chrome_row(events) == "t"


# AC 7(a)
def test_the_opening_prompt_band_carries_no_group_before_the_first_step() -> None:
    drv = _drive([
        ev("1", "quest_created", id="pazo49", title="t"),
        ev("3", "agent_message", agent_id=_STEP, provider="claude",
           model="claude-opus-5", auth_mode="subscription", stop_reason="end_turn",
           raw={"content": [{"type": "text", "text": "done"}]}, usage={}),
        _quota_event("claude"),
    ])
    # No step_started, so current_activity() is None and the toolbar takes the
    # identity fallback — even though a carrier is sitting in the builder behind it.
    assert drv._builder.current_activity() is None
    assert drv._builder.carrier_for(None) is not None
    assert drv.compose_toolbar_static() == "pazo49 · t"


# AC 7(c)
def test_the_startup_band_stays_bare_when_the_replay_established_a_carrier() -> None:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=200, status=LiveStatus(out),
                     startup_verb="resuming")
    for e in [*_cc_events(_quota_event("claude"), auth_mode="subscription"),
              *_chrome_tail()]:
        drv.on_event(e)
    assert drv._builder.carrier_for(None) is not None
    rows = drv.compose_startup(0)
    assert rows is not None and len(rows) == 3     # spinner / blank / identity
    # _cc_events' quest_created carries `quest_id`, not `id`, so the band is the
    # title alone. The boot window names the quest; it does not meter it.
    assert rows[2] == "t"

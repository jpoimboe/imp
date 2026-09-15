"""claude_code's rate-limit telemetry, normalized (spec §1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.state import InMemoryEventLog
from imp.providers.claude import ClaudeAgentRunner, quota_from_rate_limit_info
from imp.providers.quota import QuotaWindow
from tests.cc_fakes import (
    FakeClaudeSDKClient,
    FakeRateLimitInfo,
    assistant,
    rate_limit,
    result,
    system_init,
    text,
    tool_use,
)
from tests.test_claude_code_runner import _inv

# The live 2.1.259 payload, verbatim from the quest's spike capture.
_RAW = {
    "status": "allowed", "resetsAt": 1788565200, "rateLimitType": "five_hour",
    "overageStatus": "rejected", "overageDisabledReason": "org_level_disabled",
    "isUsingOverage": False,
    "unifiedWindows": {
        "five_hour": {"utilization": 0.44, "resetsAt": 1788565200},
        "seven_day": {"utilization": 0.07, "resetsAt": 1788559200},
    },
}


def test_unified_windows_normalize_to_percentages_ordered_by_duration() -> None:
    reading = quota_from_rate_limit_info(FakeRateLimitInfo(raw=_RAW))
    assert reading is not None
    assert reading.provider == "claude_code"
    # No carrier states a plan name on this side.
    assert reading.plan is None
    assert reading.windows == (
        QuotaWindow(label="5h", used_pct=44.0, resets_at=1788565200),
        QuotaWindow(label="7d", used_pct=7.0, resets_at=1788559200),
    )


def test_the_overage_window_is_never_normalized_but_survives_in_raw() -> None:
    raw = {
        **_RAW,
        "unifiedWindows": {
            **_RAW["unifiedWindows"],
            "seven_day_overage_included": {"utilization": 0.55,
                                           "resetsAt": 1788559200},
        },
    }
    reading = quota_from_rate_limit_info(FakeRateLimitInfo(raw=raw))
    assert reading is not None
    assert [w.label for w in reading.windows] == ["5h", "7d"]
    # `raw` round-trips verbatim, overage window and all (AC 5).
    assert reading.raw == raw


def test_a_payload_without_unified_windows_yields_no_reading() -> None:
    # The 0.2.87 / CLI 2.1.150 shape: everything but the per-window levels.
    old = {k: v for k, v in _RAW.items() if k != "unifiedWindows"}
    assert quota_from_rate_limit_info(FakeRateLimitInfo(raw=old)) is None


def test_a_single_window_with_no_reset_normalizes_alone() -> None:
    raw = {"status": "allowed",
           "unifiedWindows": {"seven_day": {"utilization": 0.81}}}
    reading = quota_from_rate_limit_info(FakeRateLimitInfo(raw=raw))
    assert reading is not None
    assert reading.windows == (
        QuotaWindow(label="7d", used_pct=81.0, resets_at=None),)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_a_streamed_rate_limit_event_records_the_reading(
        tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    scripted = [
        system_init("none"),
        assistant(text("done"),
                  tool_use("c1", "complete_step", {"status": "Success"})),
        rate_limit(_RAW),
        result(),
    ]

    def factory(opts: object) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    log = InMemoryEventLog()
    runner = ClaudeAgentRunner(model_name="claude-sonnet-4-5",
                                   client_factory=factory)
    events = [e async for e in runner.run(_inv(tmp_path, event_log=log))]

    # The event translates to no driver item and no warning.
    assert [e["type"] for e in events
            if e["type"] != "tool_heartbeat"] == ["agent_result"], events

    readings = [e for e in log.read() if e["type"] == "quota_reading"]
    assert len(readings) == 1
    assert readings[0]["agent_id"] == "step:p/s"
    assert readings[0]["payload"] == {
        "provider": "claude_code", "plan": None,
        "windows": [
            {"label": "5h", "used_pct": 44.0, "resets_at": 1788565200},
            {"label": "7d", "used_pct": 7.0, "resets_at": 1788559200},
        ],
        "raw": _RAW,
    }


@pytest.mark.anyio
async def test_an_event_without_unified_windows_records_nothing(
        tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    old = {k: v for k, v in _RAW.items() if k != "unifiedWindows"}
    scripted = [
        system_init("none"),
        assistant(text("done"),
                  tool_use("c1", "complete_step", {"status": "Success"})),
        rate_limit(old),
        result(),
    ]

    def factory(opts: object) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    log = InMemoryEventLog()
    runner = ClaudeAgentRunner(model_name="claude-sonnet-4-5",
                                   client_factory=factory)
    events = [e async for e in runner.run(_inv(tmp_path, event_log=log))]

    assert [e["type"] for e in events
            if e["type"] != "tool_heartbeat"] == ["agent_result"], events
    assert "quota_reading" not in [e["type"] for e in log.read()]

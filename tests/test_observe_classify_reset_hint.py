"""The retry hint derived from the last recorded quota reading (spec §6)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Literal

import pytest

import imp.providers.claude_code as cc
import imp.providers.observe_classify as ocls
from imp.engine.state import InMemoryEventLog
from imp.providers import errors as perr
from imp.providers.claude_code import ClaudeCodeAgentRunner
from imp.providers.codex_runner import CodexSession
from imp.providers.quota import QuotaReading, QuotaWindow
from tests.cc_fakes import (
    FakeClaudeSDKClient,
    assistant,
    rate_limit,
    result,
    system_init,
    text,
)
from tests.test_claude_code_runner import _inv

_NOW = 1788400000.0
_SESSION_LIMIT = "You've hit your session limit · resets 4am (America/Los_Angeles)"
_LIMIT_NO_RESET = "You have hit a weekly limit."
_WEEKLY_LIMIT = "You've hit your weekly limit · resets Jul 16, 8pm (America/Los_Angeles)"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _reading(*windows: tuple[str, float, int | None],
             provider: Literal["codex", "claude_code"] = "claude_code") -> QuotaReading:
    return QuotaReading(
        provider=provider, plan=None,
        windows=tuple(QuotaWindow(label=lb, used_pct=pct, resets_at=r)
                      for lb, pct, r in windows),
        raw={})


def test_earliest_reset_among_saturated_windows_wins() -> None:
    # Both above the floor; the 5h window resets sooner -> 30m + 120s margin.
    r = _reading(("5h", 96.0, int(_NOW) + 1800), ("7d", 91.0, int(_NOW) + 90000))
    assert ocls._hint_from_reading(r, _NOW) == 1920.0


def test_a_sole_weekly_candidate_is_chosen() -> None:
    # 5h resets first but sits below the floor -> the 7d window binds: 2h + 120s.
    r = _reading(("5h", 12.0, int(_NOW) + 600), ("7d", 94.0, int(_NOW) + 7200))
    assert ocls._hint_from_reading(r, _NOW) == 7320.0


def test_a_saturated_window_without_a_reset_is_no_candidate() -> None:
    assert ocls._hint_from_reading(_reading(("5h", 97.0, None)), _NOW) is None


def test_a_null_reset_does_not_mask_a_saturated_sibling() -> None:
    r = _reading(("5h", 97.0, None), ("7d", 93.0, int(_NOW) + 3600))
    assert ocls._hint_from_reading(r, _NOW) == 3720.0


def test_no_window_at_the_floor_produces_no_hint() -> None:
    r = _reading(("5h", 89.0, int(_NOW) + 600), ("7d", 40.0, int(_NOW) + 600))
    assert ocls._hint_from_reading(r, _NOW) is None


def test_the_floor_is_inclusive_at_ninety() -> None:
    assert ocls._hint_from_reading(
        _reading(("5h", 90.0, int(_NOW) + 600)), _NOW) == 720.0
    assert ocls._hint_from_reading(
        _reading(("5h", 89.9, int(_NOW) + 600)), _NOW) is None


def test_no_reading_produces_no_hint() -> None:
    assert ocls._hint_from_reading(None, _NOW) is None


def test_a_reset_already_past_produces_no_hint() -> None:
    assert ocls._hint_from_reading(
        _reading(("5h", 99.0, int(_NOW) - 60)), _NOW) is None


def test_a_wait_beyond_the_eight_day_ceiling_produces_no_hint() -> None:
    r = _reading(("7d", 99.0, int(_NOW) + 8 * 24 * 3600 + 1))
    assert ocls._hint_from_reading(r, _NOW) is None


def test_a_wait_exactly_at_the_ceiling_is_trusted() -> None:
    r = _reading(("7d", 99.0, int(_NOW) + 8 * 24 * 3600))
    assert ocls._hint_from_reading(r, _NOW) == 691320.0


def test_the_session_limit_branch_attaches_the_derived_hint() -> None:
    exc = ocls._classify_agent_text(
        _SESSION_LIMIT, output_tokens=0,
        reading=_reading(("5h", 96.0, int(time.time()) + 3600)))
    assert isinstance(exc, perr.RateLimitError)
    assert 3600 < exc.detail["retry_after_seconds"] <= 3721


def test_the_429_branch_attaches_the_derived_hint() -> None:
    exc = ocls._classify_agent_text(
        "API Error: 429 rate limited", output_tokens=0,
        reading=_reading(("5h", 96.0, int(time.time()) + 3600)))
    assert isinstance(exc, perr.RateLimitError)
    assert 3600 < exc.detail["retry_after_seconds"] <= 3721


def test_without_a_reading_no_hint_is_attached() -> None:
    exc = ocls._classify_agent_text(_LIMIT_NO_RESET, output_tokens=0)
    assert isinstance(exc, perr.RateLimitError)
    assert "retry_after_seconds" not in exc.detail


def test_weekly_text_only_still_classifies_as_rate_limit() -> None:
    assert isinstance(
        ocls._classify_agent_text(_WEEKLY_LIMIT, output_tokens=0), perr.RateLimitError)


def test_weekly_429_classifies_as_rate_limit_not_terminated() -> None:
    term = cc._termination_from_result(
        result(is_error=True, api_error_status=429), _WEEKLY_LIMIT, {})
    assert isinstance(term, perr.RateLimitError)
    assert not isinstance(term, perr.TerminatedError)


def test_the_wall_clock_parse_is_gone() -> None:
    for name in ("_RESET_RE", "_MONTH_ABBR", "_reset_hint_seconds",
                 "_MAX_RESET_WAIT_SECONDS"):
        assert not hasattr(ocls, name), name
    assert ocls._USAGE_LIMIT_RE.search("You've hit your weekly limit")
    assert ocls._MAX_WEEKLY_WAIT_SECONDS == 8 * 24 * 3600.0
    assert ocls._RESET_MARGIN_SECONDS == 120.0
    # The wall-clock time in the prose is no longer a hint source.
    exc = ocls._classify_agent_text(_SESSION_LIMIT, output_tokens=0)
    assert isinstance(exc, perr.RateLimitError)
    assert "retry_after_seconds" not in exc.detail


def test_the_hint_survives_the_termination_merge() -> None:
    term = cc._termination_from_result(
        result(is_error=True, api_error_status=None), _SESSION_LIMIT, {},
        reading=_reading(("5h", 96.0, int(time.time()) + 3600)))
    assert isinstance(term, perr.RateLimitError)
    assert 3600 < term.detail["retry_after_seconds"] <= 3721
    assert term.detail["is_error"] is True


@pytest.mark.anyio
async def test_a_streamed_rate_limit_event_feeds_the_retry_hint(
        tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    raw = {"status": "rejected", "rateLimitType": "five_hour",
           "unifiedWindows": {"five_hour": {"utilization": 0.96,
                                            "resetsAt": int(time.time()) + 3600}}}
    scripted = [
        system_init("none"),
        assistant(text(_SESSION_LIMIT)),
        rate_limit(raw),
        result(is_error=True, api_error_status=429),
    ]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    runner = ClaudeCodeAgentRunner(model_name="claude-sonnet-4-5",
                                   client_factory=factory)
    events = [e async for e in runner.run(
        _inv(tmp_path, event_log=InMemoryEventLog()))]

    errs = [e for e in events if e.get("type") == "error"]
    assert len(errs) == 1, events
    assert errs[0]["payload"]["category"] == "rate_limit"
    assert 3600 < errs[0]["payload"]["detail"]["retry_after_seconds"] <= 3721


@pytest.mark.anyio
async def test_codex_derives_the_hint_from_its_recorded_reading() -> None:
    session = CodexSession(None, None)
    session._proc_state = {
        "quota_reading": _reading(
            ("5h", 96.0, int(time.time()) + 3600), provider="codex"),
        "turn_output_tokens": 0,
    }

    payload = await session._terminal_error(_WEEKLY_LIMIT)

    assert payload is not None
    assert payload["category"] == "rate_limit"
    assert 3600 < payload["detail"]["retry_after_seconds"] <= 3721


@pytest.mark.anyio
async def test_codex_without_a_reading_falls_to_ordinary_backoff() -> None:
    session = CodexSession(None, None)
    session._proc_state = {"quota_reading": None, "turn_output_tokens": 0}

    payload = await session._terminal_error(_WEEKLY_LIMIT)

    assert payload is not None
    assert payload["category"] == "rate_limit"
    assert "retry_after_seconds" not in payload["detail"]

"""claude_code's rate-limit telemetry, normalized (spec §1)."""

from __future__ import annotations

from imp.providers.claude_code import quota_from_rate_limit_info
from imp.providers.quota import QuotaWindow
from tests.cc_fakes import FakeRateLimitInfo

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

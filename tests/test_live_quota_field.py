"""The position row's quota-or-cost field (spec §4)."""

from __future__ import annotations

from typing import Any

from imp.cli import narrative as nv
from imp.cli import palette as pal
from imp.cli.accounting import RowUsage
from imp.cli.live_driver import compose_position_row, quota_field
from imp.providers.quota import QuotaReading, QuotaWindow

_NOW = 1_788_000_000.0        # fixed, so every countdown is deterministic


def _act(**kw: Any) -> nv.Activity:
    base: dict[str, Any] = dict(kind="agent", model="claude-opus-5",
                                provider="claude_code", phase="implement phase")
    base.update(kw)
    return nv.Activity(**base)


def _reading(*windows: QuotaWindow) -> QuotaReading:
    return QuotaReading(provider="claude_code", plan=None, windows=windows, raw={})


def _carrier(quota: QuotaReading | None = None, *, provider: str = "claude_code",
             auth_mode: str | None = None) -> nv.Carrier:
    return nv.Carrier(provider=provider, auth_mode=auth_mode, quota=quota)


def _row(**kw: Any) -> str:
    activity = kw.pop("activity", _act())
    color = kw.pop("color", False)
    return compose_position_row(
        activity=activity, quest_id=None, title="", context_tokens=70_400,
        color=color, width=200, now_epoch=_NOW, **kw)


# AC 8
def test_the_row_renders_the_windows_after_the_context_gauge() -> None:
    left = "implement phase · claude-opus-5 · ████░░░░░░ 70k/160k"
    calm = _reading(QuotaWindow("5h", 34.0, None), QuotaWindow("7d", 7.0, None))
    row = _row(carrier=_carrier(calm, auth_mode="subscription"))
    # 53 + 110 + 35 = 198 = width - 2: the field is reached across the gap.
    assert row == left + " " * 110 + "claude_code · session 34% · week 7%"
    assert pal.display_width(row) == 198

    one_up = _reading(QuotaWindow("5h", 62.0, int(_NOW) + 2400),
                      QuotaWindow("7d", 7.0, int(_NOW) + 600000))
    assert _row(carrier=_carrier(one_up, auth_mode="subscription")).endswith(
        "claude_code · session 62% (40m left) · week 7% (7d left)")

    both = _reading(QuotaWindow("5h", 92.0, int(_NOW) + 2400),
                    QuotaWindow("7d", 96.0, int(_NOW) + 187200))
    # A red window changes nothing on the row now: the levels, and only the levels.
    red_row = _row(carrier=_carrier(both, auth_mode="subscription"))
    assert red_row.endswith("session 92% (40m left) · week 96% (2d left)")
    assert "switch provider" not in red_row


# AC 9
def test_the_bands_are_evaluated_on_the_displayed_integer() -> None:
    def field(pct: float) -> str:
        return quota_field(_reading(QuotaWindow("5h", pct, int(_NOW) + 2400)),
                           now_epoch=_NOW, color=True, over_bg=False)[1]

    assert "\x1b[32m49%" in field(49.0)      # green below 50
    assert "\x1b[33m50%" in field(50.0)      # gold from 50
    assert "\x1b[33m84%" in field(84.0)      # gold through 84
    assert "\x1b[31m85%" in field(85.0)      # red from 85
    assert "\x1b[33m50%" in field(49.5)      # rounded half-up, then banded
    assert "\x1b[31m85%" in field(84.5)      # banker's round() would say 84: gold
    # The countdown is always on: a known future reset shows at any percentage.
    assert "(40m left)" in field(49.0)
    assert "(40m left)" in field(50.0)
    # The label is uncolored and the countdown gray; only the percentage is hued.
    assert field(50.0).startswith("session \x1b[33m50%")
    assert field(50.0).endswith("\x1b[90m(40m left)\x1b[0m")


# AC 13
def test_an_unknown_or_past_reset_renders_the_percentage_alone() -> None:
    # Percentage-independent, now that the band gates nothing: calm and red alike.
    for pct, shown in ((92.0, "session 92%"), (34.0, "session 34%")):
        null_reset = _reading(QuotaWindow("5h", pct, None))
        assert _row(carrier=_carrier(null_reset, auth_mode="subscription")).endswith(shown)

        already_past = _reading(QuotaWindow("5h", pct, int(_NOW) - 10))
        assert _row(carrier=_carrier(already_past, auth_mode="subscription")).endswith(shown)


# AC 10
def test_a_metered_backend_renders_the_bare_quest_total() -> None:
    priced = RowUsage(dollars=4.21, priced=1)
    row = _row(carrier=_carrier(auth_mode="apikey"), cost=priced)
    assert row == "implement phase · claude-opus-5 · ████░░░░░░ 70k/160k" + " " * 140 + "$4.21"
    assert " · $4.21" not in row                 # flush right, no label, no color, no ~
    assert pal.display_width(row) == 198
    # A step on a model the price table lacks still renders the figure, no ≥.
    with_unpriced = RowUsage(dollars=4.21, priced=1, unpriced=1)
    assert _row(carrier=_carrier(auth_mode="apikey"), cost=with_unpriced).endswith(" $4.21")
    assert _row(carrier=_carrier(auth_mode="apikey"),
                cost=RowUsage(dollars=0.004, priced=1)).endswith(" <$0.01")
    # An API-only provider carries no auth_mode and is metered all the same.
    assert _row(carrier=_carrier(provider="anthropic"),
                cost=priced).endswith(" $4.21")
    # AC 5: a recorded reading never stamps a provider segment onto the metered field.
    metered = _row(carrier=_carrier(_reading(QuotaWindow("5h", 34.0, None)),
                                    auth_mode="apikey"), cost=priced)
    assert metered.endswith(" $4.21") and "claude_code" not in metered


# AC 10
def test_nothing_priced_yet_renders_no_field_rather_than_a_placeholder() -> None:
    row = _row(carrier=_carrier(_reading(QuotaWindow("5h", 34.0, None)),
                                auth_mode="apikey"), cost=RowUsage())
    assert row.endswith("claude-opus-5 · ████░░░░░░ 70k/160k")
    assert "n/a" not in row and "—" not in row
    # The metered choice is exclusive: a stale reading never leaks onto this row.
    assert "session" not in row and "week" not in row


# AC 11
def test_a_subscription_backend_with_no_reading_renders_no_field() -> None:
    row = _row(carrier=_carrier(auth_mode="subscription"))
    assert row == "implement phase · claude-opus-5 · ████░░░░░░ 70k/160k"


def test_an_unrecognized_window_label_renders_verbatim() -> None:
    odd = _reading(QuotaWindow("30d", 12.0, None))
    assert _row(carrier=_carrier(odd, auth_mode="subscription")).endswith("30d 12%")


def test_the_countdown_rounds_to_one_nearest_unit() -> None:
    def left(seconds: int) -> str:
        return quota_field(_reading(QuotaWindow("5h", 92.0, int(_NOW) + seconds)),
                           now_epoch=_NOW, color=False, over_bg=False)[1]

    assert left(9960) == "session 92% (3h left)"       # 2h46m -> nearest hour
    assert left(253800) == "session 92% (3d left)"     # 2d23h -> nearest day
    assert left(2340) == "session 92% (39m left)"      # under an hour: minutes
    assert left(20) == "session 92% (1m left)"         # never rounds down to 0m
    assert left(3570) == "session 92% (1h left)"       # 59m30s promotes, not 60m
    assert left(86340) == "session 92% (1d left)"      # 23h59m promotes, not 24h


# AC 1, AC 2
def test_a_calm_window_with_a_known_reset_still_shows_its_countdown() -> None:
    calm_future = _reading(QuotaWindow("5h", 34.0, int(_NOW) + 9960),
                           QuotaWindow("7d", 7.0, int(_NOW) + 430000))
    full, windows = quota_field(calm_future, now_epoch=_NOW, color=False,
                                over_bg=False)
    assert windows == "session 34% (3h left) · week 7% (5d left)"
    assert full == "claude_code · " + windows
    # The provider name, the separator after it, and the labels carry no SGR
    # sequence of their own, not even a reset.
    colored = quota_field(calm_future, now_epoch=_NOW, color=True,
                          over_bg=False)[0]
    assert colored == (
        "claude_code · session \x1b[32m34%\x1b[0m \x1b[90m(3h left)\x1b[0m"
        " · week \x1b[32m7%\x1b[0m \x1b[90m(5d left)\x1b[0m")


# AC 1
def test_the_field_names_the_readings_own_provider() -> None:
    codex = QuotaReading(provider="codex", plan=None, raw={},
                         windows=(QuotaWindow("5h", 62.0, int(_NOW) + 10800),
                                  QuotaWindow("7d", 91.0, int(_NOW) + 432000)))
    full, windows = quota_field(codex, now_epoch=_NOW, color=False, over_bg=False)
    assert full == "codex · session 62% (3h left) · week 91% (5d left)"
    assert windows == "session 62% (3h left) · week 91% (5d left)"
    # The name is the reading's, never the activity's: a claude_code step whose
    # recorded reading came from codex still says codex.
    row = _row(activity=_act(provider="claude_code"),
               carrier=_carrier(codex, auth_mode="subscription"))
    assert row.endswith("codex · session 62% (3h left) · week 91% (5d left)")


# AC 3
def test_the_provider_segment_is_the_first_rung_the_row_sheds() -> None:
    reading = _reading(QuotaWindow("5h", 34.0, int(_NOW) + 2400),
                       QuotaWindow("7d", 7.0, int(_NOW) + 518400))

    def at(width: int) -> str:
        return compose_position_row(
            activity=_act(), quest_id=None, title="", context_tokens=70_400,
            color=False, width=width, now_epoch=_NOW,
            carrier=_carrier(reading, auth_mode="subscription"))

    # (a) target 198: left group 53 + full right group 56, gap to spare.
    assert at(200).endswith(
        "claude_code · session 34% (40m left) · week 7% (6d left)")
    # (b) target 98 fits the windows (42) but not the provider segment — and the
    # countdowns come with them, there being no third candidate to shed.
    windows_only = at(100)
    assert windows_only.endswith("session 34% (40m left) · week 7% (6d left)")
    assert "claude_code" not in windows_only
    assert pal.display_width(windows_only) == 98
    # (c) target 78 fits neither candidate: the quota goes whole, the gauge stays.
    bare = at(80)
    assert bare.endswith("claude-opus-5 · ████░░░░░░ 70k/160k")
    assert "session" not in bare


# AC 4
def test_the_position_row_carries_no_switch_hint_at_any_level() -> None:
    import imp.cli.live_driver as ld

    assert not hasattr(ld, "SWITCH_HINT")
    at_red = _reading(QuotaWindow("5h", float(pal.QUOTA_RED_AT), None))
    row = _row(carrier=_carrier(at_red, auth_mode="subscription"))
    assert row.endswith(f"session {pal.QUOTA_RED_AT}%")
    assert "switch provider" not in row


# AC 1, AC 3, AC 6
def test_a_provider_less_activity_still_renders_the_carriers_group() -> None:
    chrome = nv.Activity(kind="script", verb="pytest -q")
    windows = _reading(QuotaWindow("5h", 62.0, int(_NOW) + 9960),
                       QuotaWindow("7d", 7.0, int(_NOW) + 430000))
    row = _row(activity=chrome,
               carrier=_carrier(windows, auth_mode="subscription"))
    assert row.endswith("claude_code · session 62% (3h left) · week 7% (5d left)")
    assert "█" not in row and "░" not in row      # kind-gated: no gauge on a chrome row
    # The metered choice reads the carrier too, not the provider-less activity.
    metered = _row(activity=chrome, carrier=_carrier(provider="anthropic"),
                   cost=RowUsage(dollars=4.21, priced=1))
    assert metered.endswith(" $4.21") and "session" not in metered

"""The position row's quota-or-cost field (spec §4)."""

from __future__ import annotations

from typing import Any

from imp.cli import narrative as nv
from imp.cli.accounting import RowUsage
from imp.cli.live_driver import compose_position_row
from imp.providers.quota import QuotaReading, QuotaWindow

_NOW = 1_788_000_000.0        # fixed, so every countdown is deterministic


def _act(**kw: Any) -> nv.Activity:
    base: dict[str, Any] = dict(kind="agent", model="claude-opus-5",
                                provider="claude_code", phase="implement phase")
    base.update(kw)
    return nv.Activity(**base)


def _reading(*windows: QuotaWindow) -> QuotaReading:
    return QuotaReading(provider="claude_code", plan=None, windows=windows, raw={})


def _row(**kw: Any) -> str:
    activity = kw.pop("activity", _act())
    color = kw.pop("color", False)
    return compose_position_row(
        activity=activity, quest_id=None, title="", prompt_tokens=None,
        color=color, width=200, context_pct=44.0, now_epoch=_NOW, **kw)


# AC 8
def test_the_row_renders_the_windows_after_the_context_gauge() -> None:
    calm = _reading(QuotaWindow("5h", 34.0, None), QuotaWindow("7d", 7.0, None))
    assert _row(auth_mode="subscription", quota=calm) == (
        "implement phase · claude-opus-5 · ████░░░░░░ 44% · quota 5h 34%, 7d 7%")

    one_up = _reading(QuotaWindow("5h", 62.0, int(_NOW) + 2400),
                      QuotaWindow("7d", 7.0, int(_NOW) + 600000))
    assert _row(auth_mode="subscription", quota=one_up).endswith(
        "████░░░░░░ 44% · quota 5h 62% (40m), 7d 7%")

    both = _reading(QuotaWindow("5h", 92.0, int(_NOW) + 2400),
                    QuotaWindow("7d", 96.0, int(_NOW) + 187200))
    assert _row(auth_mode="subscription", quota=both).endswith(
        "████░░░░░░ 44% · quota 5h 92% (40m), 7d 96% (2d 4h)")


# AC 9
def test_the_bands_are_evaluated_on_the_displayed_integer() -> None:
    def field(pct: float) -> str:
        row = _row(color=True, auth_mode="subscription",
                   quota=_reading(QuotaWindow("5h", pct, int(_NOW) + 2400)))
        return row.split(" · ")[-1]

    assert "\x1b[32m49%" in field(49.0)      # green below 50
    assert "\x1b[33m50%" in field(50.0)      # gold from 50
    assert "\x1b[33m84%" in field(84.0)      # gold through 84
    assert "\x1b[31m85%" in field(85.0)      # red from 85
    assert "\x1b[33m50%" in field(49.5)      # rounded half-up, then banded
    assert "\x1b[31m85%" in field(84.5)      # banker's round() would say 84: gold
    # The countdown is a trouble marker: it appears with the gold band, not before.
    assert "(40m)" not in field(49.0)
    assert "(40m)" in field(50.0)
    # quota, the label and the countdown are gray; only the percentage is hued.
    assert field(50.0).startswith("\x1b[90mquota\x1b[0m \x1b[90m5h\x1b[0m ")
    assert field(50.0).endswith("\x1b[90m(40m)\x1b[0m")


# AC 13
def test_an_unknown_or_past_reset_renders_the_percentage_alone() -> None:
    null_reset = _reading(QuotaWindow("5h", 92.0, None))
    assert _row(auth_mode="subscription", quota=null_reset).endswith("quota 5h 92%")

    already_past = _reading(QuotaWindow("5h", 92.0, int(_NOW) - 10))
    assert _row(auth_mode="subscription", quota=already_past).endswith("quota 5h 92%")


# AC 10
def test_a_metered_backend_renders_the_bare_quest_total() -> None:
    priced = RowUsage(dollars=4.21, priced=1)
    assert _row(auth_mode="apikey", cost=priced).endswith(
        "████░░░░░░ 44% · $4.21")                    # no label, no color, no ~
    # A step on a model the price table lacks still renders the figure, no ≥.
    with_unpriced = RowUsage(dollars=4.21, priced=1, unpriced=1)
    assert _row(auth_mode="apikey", cost=with_unpriced).endswith(" · $4.21")
    assert _row(auth_mode="apikey", cost=RowUsage(dollars=0.004,
                                                  priced=1)).endswith(" · <$0.01")
    # An API-keyed provider carries no auth_mode and is metered all the same.
    assert _row(activity=_act(provider="anthropic"), cost=priced).endswith(" · $4.21")


# AC 10
def test_nothing_priced_yet_renders_no_field_rather_than_a_placeholder() -> None:
    row = _row(auth_mode="apikey", cost=RowUsage(),
               quota=_reading(QuotaWindow("5h", 34.0, None)))
    assert row.endswith("claude-opus-5 · ████░░░░░░ 44%")
    assert "n/a" not in row and "—" not in row
    # The metered choice is exclusive: a stale reading never leaks onto this row.
    assert "quota" not in row


# AC 11
def test_a_subscription_backend_with_no_reading_renders_no_field() -> None:
    row = _row(auth_mode="subscription")
    assert row == "implement phase · claude-opus-5 · ████░░░░░░ 44%"

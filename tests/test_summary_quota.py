"""The `imp log -s` quota block: the aggregate build_summary accumulates and the
from→to rows both renderers draw from it (spec §5)."""

from __future__ import annotations

from imp.cli.render_markdown import render_summary_markdown
from imp.cli.render_summary import SummaryHeader, build_summary, render_summary

_HDR = SummaryHeader(workflow="default", quest_id="pazo49", status="merged",
                     total_seconds=300.0)


def _reading(provider: str, *windows: tuple[str, float, int | None],
             ts: str = "2026-01-01T00:00:00+00:00") -> dict:
    return {"type": "quota_reading", "timestamp": ts,
            "payload": {"provider": provider, "plan": None,
                        "windows": [{"label": label, "used_pct": pct,
                                     "resets_at": resets_at}
                                    for label, pct, resets_at in windows],
                        "raw": {}}}


def _step(ts: str = "2026-01-01T00:00:00+00:00") -> list[dict]:
    return [
        {"type": "step_started", "timestamp": ts,
         "payload": {"step": "coder", "phase": "implement"}},
        {"type": "step_completed", "timestamp": "2026-01-01T00:05:00+00:00",
         "payload": {"step": "coder", "action": {"verb": "continue"}}},
    ]


# AC 17 (producer)
def test_build_summary_carries_first_and_last_reading_per_window() -> None:
    summary = build_summary([
        *_step(),
        _reading("claude_code", ("5h", 44.0, 100), ("7d", 7.0, 900)),
        _reading("claude_code", ("5h", 51.0, 100), ("7d", 8.0, 900)),
        _reading("claude_code", ("5h", 58.0, 100), ("7d", 9.0, 900)),
    ])
    assert [r.step for r in summary.rows] == ["coder"]
    (cc,) = summary.quota
    assert cc.provider == "claude_code"
    assert [(w.label, w.first_pct, w.last_pct, w.across_reset)
            for w in cc.windows] == [("5h", 44.0, 58.0, None),
                                     ("7d", 7.0, 9.0, None)]


def test_readings_are_kept_per_provider_in_first_seen_order() -> None:
    summary = build_summary([
        _reading("claude_code", ("5h", 44.0, 100)),
        _reading("codex", ("5h", 12.0, 200)),
        _reading("claude_code", ("5h", 78.0, 100)),
        _reading("codex", ("5h", 20.0, 200)),
    ])
    assert [(q.provider, q.windows[0].first_pct, q.windows[0].last_pct)
            for q in summary.quota] == [("claude_code", 44.0, 78.0),
                                        ("codex", 12.0, 20.0)]


def test_no_readings_leaves_the_aggregate_empty() -> None:
    assert build_summary(_step()).quota == ()


# AC 18
def test_a_reset_crossing_accumulates_the_post_reset_level() -> None:
    summary = build_summary([
        _reading("codex", ("5h", 88.0, 100), ("7d", 40.0, 900)),
        _reading("codex", ("5h", 95.0, 100), ("7d", 42.0, 900)),
        _reading("codex", ("5h", 4.0, 20000), ("7d", 43.0, 900)),
        _reading("codex", ("5h", 12.0, 20000), ("7d", 44.0, 900)),
    ])
    (codex,) = summary.quota
    five, seven = codex.windows
    # +7 (88→95), +4 (the post-reset level), +8 (4→12).
    assert (five.first_pct, five.last_pct, five.across_reset) == (88.0, 12.0, 19.0)
    assert (seven.first_pct, seven.last_pct, seven.across_reset) == (40.0, 44.0, None)


def test_resets_at_jitter_without_a_level_drop_is_not_a_reset() -> None:
    """Real codex payloads re-state resets_at +/-1s between records while the level
    climbs; treating that as a boundary would add the whole level each time."""
    summary = build_summary([
        _reading("codex", ("7d", 17.0, 1788971433)),
        _reading("codex", ("7d", 19.0, 1788971434)),
        _reading("codex", ("7d", 22.0, 1788971433)),
        _reading("codex", ("7d", 27.0, 1788971434)),
    ])
    (window,) = summary.quota[0].windows
    assert window.across_reset is None
    assert (window.first_pct, window.last_pct) == (17.0, 27.0)


def test_a_level_drop_within_the_jitter_tolerance_is_not_a_reset() -> None:
    """A wobbling level whose reset time did not move on has not rolled over."""
    summary = build_summary([
        _reading("codex", ("5h", 44.0, 1788971433)),
        _reading("codex", ("5h", 43.0, 1788971434)),
        _reading("codex", ("5h", 47.0, 1788971434)),
    ])
    (window,) = summary.quota[0].windows
    assert window.across_reset is None


def test_a_window_that_rolls_over_barely_used_is_still_a_reset() -> None:
    """The reset time moved on by five hours; the level reads the same on both
    sides because almost nothing was spent. Requiring a drop would miss it."""
    summary = build_summary([
        _reading("codex", ("5h", 1.0, 1788971433)),
        _reading("codex", ("5h", 1.0, 1788971433 + 18000)),
        _reading("codex", ("5h", 6.0, 1788971433 + 18000)),
    ])
    (window,) = summary.quota[0].windows
    # 1 consumed since the boundary, then +5 on top of it.
    assert window.across_reset == 6.0
    assert (window.first_pct, window.last_pct) == (1.0, 6.0)


def test_windows_are_ordered_by_duration_however_they_first_arrived() -> None:
    """claude drops a window whose reset has passed, so the first reading of a run
    can carry only 7d."""
    summary = build_summary([
        _reading("claude_code", ("7d", 7.0, 900)),
        _reading("claude_code", ("5h", 44.0, 100), ("7d", 9.0, 900)),
    ])
    assert [w.label for w in summary.quota[0].windows] == ["5h", "7d"]


# AC 17 (a), (c) and AC 18 rendering
def test_the_block_renders_after_total_with_bands_on_the_to_value() -> None:
    summary = build_summary([
        *_step(),
        _reading("claude_code", ("5h", 44.0, 100), ("7d", 7.0, 900)),
        _reading("claude_code", ("5h", 58.0, 100), ("7d", 9.0, 900)),
    ])
    lines = render_summary(summary.rows, header=_HDR, color=False, width=100,
                           quota=summary.quota)
    assert lines[-1] == "  quota   claude_code  ·  5h 44% → 58%, 7d 7% → 9%"
    assert lines[-2].startswith("  total")
    colored = render_summary(summary.rows, header=_HDR, color=True, width=100,
                             quota=summary.quota)[-1]
    # The from value is plain and the to value carries the gold band; both labels gray.
    assert " 44% → \x1b[33m58%\x1b[0m" in colored
    assert "\x1b[90m5h\x1b[0m" in colored
    assert "\x1b[90mquota\x1b[0m" in colored
    assert "\x1b[90mclaude_code\x1b[0m" in colored


def test_the_heading_appears_once_and_providers_align() -> None:
    summary = build_summary([
        _reading("claude_code", ("5h", 44.0, 100), ("7d", 81.0, 900)),
        _reading("claude_code", ("5h", 78.0, 100), ("7d", 96.0, 900)),
        _reading("codex", ("5h", 12.0, 200), ("7d", 3.0, 800)),
        _reading("codex", ("5h", 20.0, 200), ("7d", 5.0, 800)),
    ])
    lines = render_summary(summary.rows, header=_HDR, color=False, width=100,
                           quota=summary.quota)
    assert lines[-2:] == [
        "  quota   claude_code  ·  5h 44% → 78%, 7d 81% → 96%",
        "          codex        ·  5h 12% → 20%, 7d 3% → 5%",
    ]


def test_a_reset_crossing_renders_its_accumulated_total() -> None:
    summary = build_summary([
        _reading("codex", ("5h", 88.0, 100), ("7d", 40.0, 900)),
        _reading("codex", ("5h", 95.0, 100), ("7d", 42.0, 900)),
        _reading("codex", ("5h", 4.0, 20000), ("7d", 43.0, 900)),
        _reading("codex", ("5h", 12.0, 20000), ("7d", 44.0, 900)),
    ])
    lines = render_summary(summary.rows, header=_HDR, color=False, width=100,
                           quota=summary.quota)
    assert lines[-1] == ("  quota   codex  ·  5h 88% → 12% (+19 across reset), "
                         "7d 40% → 44%")
    colored = render_summary(summary.rows, header=_HDR, color=True, width=100,
                             quota=summary.quota)[-1]
    assert "\x1b[90m(+19 across reset)\x1b[0m" in colored


# AC 20 (a)
def test_a_fully_metered_run_renders_the_legend_and_no_quota_block() -> None:
    """No quota reading → block absent; total row and legend render as today."""
    priced = {"type": "agent_message", "agent_id": "a1",
              "timestamp": "2026-01-01T00:01:00+00:00",
              "payload": {"provider": "anthropic", "model": "claude-sonnet-4-6",
                          "usage": {"input_tokens": 20000, "output_tokens": 900}}}
    events = [_step()[0], {"type": "step_context", "agent_id": "a1",
                           "timestamp": "2026-01-01T00:00:30+00:00", "payload": {}},
              priced, _step()[1]]
    summary = build_summary(events)
    assert summary.quota == ()
    lines = render_summary(summary.rows, header=_HDR, color=False, width=100,
                           quota=summary.quota)
    assert not any("quota" in ln for ln in lines)
    # The legend still closes the block, immediately after `total`.
    assert lines[-1] == "  ~ estimated at API list price"
    assert lines[-2].startswith("  total") and lines[-2].endswith("~$0.07")
    assert lines == render_summary(summary.rows, header=_HDR, color=False,
                                   width=100)


def test_the_block_leaves_the_numeric_columns_byte_identical() -> None:
    events = [*_step(),
              _reading("claude_code", ("5h", 44.0, 100), ("7d", 7.0, 900)),
              _reading("claude_code", ("5h", 58.0, 100), ("7d", 9.0, 900))]
    summary = build_summary(events)
    without = render_summary(summary.rows, header=_HDR, color=False, width=100)
    with_quota = render_summary(summary.rows, header=_HDR, color=False,
                                width=100, quota=summary.quota)
    assert with_quota[:len(without)] == without
    assert len(with_quota) == len(without) + 1


# AC 19
def test_markdown_carries_the_same_rows() -> None:
    summary = build_summary([
        *_step(),
        _reading("claude_code", ("5h", 44.0, 100), ("7d", 7.0, 900)),
        _reading("claude_code", ("5h", 58.0, 100), ("7d", 9.0, 900)),
        _reading("codex", ("5h", 12.0, 200)),
        _reading("codex", ("5h", 20.0, 200)),
    ])
    out = render_summary_markdown(summary.rows, header=_HDR,
                                  quota=summary.quota)
    assert "**quota**" in out
    assert "- claude_code · 5h 44% → 58%, 7d 7% → 9%" in out
    assert "- codex · 5h 12% → 20%" in out
    assert "\x1b[" not in out
    # Ordering: total row → **quota** → first bullet; fixture is unpriced so no legend.
    assert out.index("| total |") < out.index("**quota**") < out.index("- claude_code")


def test_markdown_of_a_run_with_no_readings_carries_no_block() -> None:
    summary = build_summary(_step())
    out = render_summary_markdown(summary.rows, header=_HDR,
                                  quota=summary.quota)
    assert "quota" not in out

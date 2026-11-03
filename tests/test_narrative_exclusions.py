"""Paused and human-wait time excluded from scrollback durations."""
from __future__ import annotations

from imp.cli import narrative as nv
from tests._event_fixtures import ev


def _ts(secs: int) -> str:
    return f"2026-01-01T00:{secs // 60:02d}:{secs % 60:02d}+00:00"


def test_tracker_closes_spans_incrementally_as_events_arrive() -> None:
    tracker = nv.ExclusionTracker()
    tracker.feed(ev("1", "step_started", step="verify", phase="implement", timestamp=_ts(0)))
    tracker.feed(ev("2", "workflow_paused", reason="ctrl-c", timestamp=_ts(10)))
    assert tracker.exclusions.spans == ()        # still open: nothing excluded yet
    tracker.feed(ev("3", "step_started", step="verify", phase="implement", timestamp=_ts(175)))
    assert tracker.exclusions.spans == ((_ts(10), _ts(175)),)
    assert tracker.exclusions.overlap(_ts(0), _ts(195)) == 165.0

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from imp.cli import narrative as nv
from imp.cli.live_driver import compose_activity_line


def test_dependency_wait_line_shows_verb_elapsed_and_pause_hint() -> None:
    start = datetime(2026, 6, 3, 0, 0, 0, tzinfo=UTC)
    now = start + timedelta(minutes=4)
    act = nv.Activity(verb="waiting for tuhe55, koze82 to merge",
                      dep_wait=True, start_ts=start.isoformat())
    line = compose_activity_line(activity=act, frame=0, color=False,
                                 now_iso=now.isoformat())
    assert "waiting for tuhe55, koze82 to merge" in line
    assert "4m 0s" in line
    assert "press ctrl-d to pause" in line


def test_non_dependency_wait_line_has_no_pause_hint() -> None:
    act = nv.Activity(verb="waiting to merge (2 ahead)", lane_wait=True,
                      start_ts="2026-06-03T00:00:00+00:00")
    line = compose_activity_line(activity=act, frame=0, color=False,
                                 now_iso="2026-06-03T00:00:10+00:00")
    assert "press ctrl-d to pause" not in line

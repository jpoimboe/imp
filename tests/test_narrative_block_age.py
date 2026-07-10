# tests/test_narrative_block_age.py
from __future__ import annotations

from imp.cli import narrative as nv


def test_activity_elapsed_zero_when_start_ts_missing() -> None:
    act = nv.Activity(verb="thinking")            # no start_ts
    assert nv.activity_elapsed(act, "2026-01-01T00:00:10+00:00") == 0.0


def test_activity_elapsed_counts_from_start_ts() -> None:
    act = nv.Activity(verb="thinking", start_ts="2026-01-01T00:00:00+00:00")
    assert nv.activity_elapsed(act, "2026-01-01T00:00:07+00:00") == 7.0


def test_activity_elapsed_excludes_source_backoff_beyond_base() -> None:
    # A builder that has accrued 4s of completed backoff; the block began when
    # 1s had already accrued, so only 3s of backoff falls inside the block.
    src = nv.NarrativeBuilder()
    src._backoff_wait_accum = 4.0
    act = nv.Activity(verb="thinking", start_ts="2026-01-01T00:00:00+00:00",
                      backoff_source=src, backoff_base=1.0)
    # 10s wall span − (4 − 1) within-block backoff = 7s honest.
    assert nv.activity_elapsed(act, "2026-01-01T00:00:10+00:00") == 7.0


def test_activity_elapsed_never_negative() -> None:
    src = nv.NarrativeBuilder()
    src._backoff_wait_accum = 100.0
    act = nv.Activity(verb="thinking", start_ts="2026-01-01T00:00:00+00:00",
                      backoff_source=src, backoff_base=0.0)
    assert nv.activity_elapsed(act, "2026-01-01T00:00:05+00:00") == 0.0

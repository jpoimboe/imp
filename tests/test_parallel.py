from __future__ import annotations

import pytest

from imp.agents.parallel import (
    DEFAULT_MAX_PARALLEL_TASKS,
    _resolve,
    max_parallel_tasks,
    reset_parallelism_cache,
)


def test_default_when_env_absent() -> None:
    """No env var -> the default band-top (8), no warning."""
    s = _resolve(None)
    assert s.value == DEFAULT_MAX_PARALLEL_TASKS == 8
    assert s.warning is None


@pytest.mark.parametrize("raw", ["0", "-3", "abc", "", "3.5"])
def test_low_or_garbage_clamps_to_one_with_warning(raw: str) -> None:
    """AC22: =0 and non-integer input clamp to 1, each with a runner_warning."""
    s = _resolve(raw)
    assert s.value == 1
    assert s.warning is not None
    assert "IMP_MAX_PARALLEL_TASKS" in s.warning


def test_high_clamps_to_sixty_four_with_warning() -> None:
    """AC22: =800 clamps to 64 with a runner_warning."""
    s = _resolve("800")
    assert s.value == 64
    assert s.warning is not None
    assert "64" in s.warning


@pytest.mark.parametrize("raw,expected", [("1", 1), ("4", 4), ("64", 64)])
def test_in_range_passes_through_without_warning(raw: str, expected: int) -> None:
    s = _resolve(raw)
    assert s.value == expected
    assert s.warning is None


def test_accessor_reads_once_then_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC22: the knob is resolved ONCE; a mid-process env change is not seen
    until the accessor seam is reset (tests override through the seam, never by
    mutating env mid-process)."""
    reset_parallelism_cache()
    monkeypatch.setenv("IMP_MAX_PARALLEL_TASKS", "3")
    assert max_parallel_tasks() == 3
    monkeypatch.setenv("IMP_MAX_PARALLEL_TASKS", "5")
    assert max_parallel_tasks() == 3  # cached: not re-read
    reset_parallelism_cache()
    assert max_parallel_tasks() == 5  # seam reset -> re-resolved
    reset_parallelism_cache()

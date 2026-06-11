from __future__ import annotations

import pytest

from imp.agents.parallel import (
    DEFAULT_MAX_PARALLEL_TASKS,
    ParallelismSetting,
    _resolve,
    emit_parallelism_warning_once,
    max_parallel_tasks,
    reset_parallelism_cache,
)
from imp.engine.state import InMemoryEventLog


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


def test_emit_warning_once_appends_single_runner_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clamp warning is emitted exactly once, as a runner_warning carrying the
    clamp message."""
    reset_parallelism_cache()
    import imp.agents.parallel as parallel_mod
    monkeypatch.setattr(
        parallel_mod, "_resolved",
        ParallelismSetting(1, "IMP_MAX_PARALLEL_TASKS=0 below minimum; clamped to 1"),
    )
    log = InMemoryEventLog()
    emit_parallelism_warning_once(log, agent_id="step:plan/p")
    emit_parallelism_warning_once(log, agent_id="step:plan/p")  # no-op second time
    warnings = [e for e in log.read() if e["type"] == "runner_warning"]
    assert len(warnings) == 1
    assert "clamped to 1" in warnings[0]["payload"]["message"]
    reset_parallelism_cache()


def test_emit_warning_once_silent_when_no_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An in-range (or absent) knob produces no runner_warning event."""
    reset_parallelism_cache()
    import imp.agents.parallel as parallel_mod
    monkeypatch.setattr(parallel_mod, "_resolved", ParallelismSetting(8, None))
    log = InMemoryEventLog()
    emit_parallelism_warning_once(log, agent_id="step:plan/p")
    assert [e for e in log.read() if e["type"] == "runner_warning"] == []
    reset_parallelism_cache()

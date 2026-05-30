"""Fix H: provider-agnostic loop detector (repeated-failure + no-progress)."""

from __future__ import annotations

from imp.agents.loop_guard import (
    DEFAULT_NOPROGRESS_N,
    DEFAULT_REPEAT_K,
    LoopGuard,
    ToolOutcome,
    TurnRecord,
)


def _err_turn(msg: str) -> TurnRecord:
    return TurnRecord(assistant_text="", tools=[], has_complete_step=False,
                      error_category="api", error_kind="http_400",
                      error_message=msg)


def _text_turn(text: str) -> TurnRecord:
    return TurnRecord(assistant_text=text, tools=[], has_complete_step=False)


def _write_turn(tool_use_id: str) -> TurnRecord:
    return TurnRecord(
        assistant_text="", has_complete_step=False,
        tools=[ToolOutcome(name="write_file", tool_use_id=tool_use_id,
                           input={"path": "a.py"}, is_error=False)])


def test_repeated_identical_error_trips_at_k() -> None:
    g = LoopGuard()
    assert g.observe(_err_turn("API Error: 400 x")) is None  # 1
    assert g.observe(_err_turn("API Error: 400 x")) is None  # 2
    err = g.observe(_err_turn("API Error: 400 x"))           # 3 == K
    assert err is not None
    assert err.category == "loop"
    assert getattr(err, "kind", "") == "repeated_failure"


def test_novel_signature_resets_repeat_counter() -> None:
    g = LoopGuard()
    g.observe(_err_turn("API Error: 400 x"))
    g.observe(_err_turn("API Error: 400 x"))
    g.observe(_err_turn("API Error: 400 DIFFERENT"))  # resets to 1
    assert g.observe(_err_turn("API Error: 400 DIFFERENT")) is None  # 2 < K


def test_no_progress_trips_at_n_with_distinct_text() -> None:
    g = LoopGuard()
    # Distinct text each turn -> repeat counter never reaches K, but no-progress
    # accumulates.
    last = None
    for i in range(DEFAULT_NOPROGRESS_N):
        last = g.observe(_text_turn(f"thinking step {i}"))
    assert last is not None
    assert getattr(last, "kind", "") == "no_progress"


def test_progress_resets_both_counters() -> None:
    g = LoopGuard()
    for i in range(DEFAULT_NOPROGRESS_N - 1):
        g.observe(_text_turn(f"thinking {i}"))
    assert g.observe(_write_turn("t-new")) is None      # progress resets
    # Now it takes another full N non-progress turns to trip.
    last = None
    for i in range(DEFAULT_NOPROGRESS_N):
        last = g.observe(_text_turn(f"more {i}"))
    assert last is not None


def test_complete_step_is_progress() -> None:
    g = LoopGuard()
    rec = TurnRecord(assistant_text="", tools=[], has_complete_step=True)
    assert g.observe(rec) is None


def _read_turn(i: int) -> TurnRecord:
    return TurnRecord(
        assistant_text="", has_complete_step=False,
        tools=[ToolOutcome(name="read_file", tool_use_id=f"r{i}",
                           input={"path": f"f{i}.py"}, is_error=False)])


def test_read_only_tools_are_not_progress() -> None:
    # read_file/grep/glob are NOT in WRITE_CLASS_TOOLS, so a run of pure
    # exploration turns DOES accumulate no_progress.
    g = LoopGuard()
    last = None
    for i in range(DEFAULT_NOPROGRESS_N):
        last = g.observe(_read_turn(i))
    assert last is not None
    assert getattr(last, "kind", "") == "no_progress"


def test_read_exploration_then_write_does_not_trip() -> None:
    g = LoopGuard()
    for i in range(DEFAULT_NOPROGRESS_N - 1):
        assert g.observe(_read_turn(i)) is None
    assert g.observe(_write_turn("w1")) is None        # write resets
    for i in range(DEFAULT_NOPROGRESS_N - 1):
        assert g.observe(_read_turn(100 + i)) is None  # below threshold again


def test_healthy_distinct_writes_never_trip() -> None:
    g = LoopGuard()
    for i in range(DEFAULT_NOPROGRESS_N + DEFAULT_REPEAT_K + 5):
        assert g.observe(_write_turn(f"t{i}")) is None


def test_thresholds_are_constructor_overridable() -> None:
    g = LoopGuard(repeat_k=2, noprogress_n=3)
    g.observe(_err_turn("same"))
    assert g.observe(_err_turn("same")) is not None  # K=2


def test_fresh_guard_does_not_trip_on_first_turn() -> None:
    # A resumed invocation (large prior transcript) must not trip on the first
    # NEW turn. The guard holds per-invocation state only and is constructed
    # fresh per run() — so a fresh guard's first observe is always None.
    assert LoopGuard().observe(_err_turn("first turn after resume")) is None
    assert LoopGuard().observe(_text_turn("resumed prose")) is None


def test_new_errored_write_id_is_progress_then_same_id_is_not() -> None:
    # A write-class tool whose RESULT errored still counts as progress the first
    # time its tool_use_id is seen (a fresh write attempt is forward motion).
    # The SAME id on later turns is no longer progress, so no_progress then
    # accumulates normally.
    g = LoopGuard()
    errored_write = TurnRecord(
        assistant_text="", has_complete_step=False,
        tools=[ToolOutcome(name="write_file", tool_use_id="w1",
                           input={"path": "a.py"}, is_error=True)])
    # First sighting of id "w1": progress -> None, and it should reset/keep the
    # no_progress counter from tripping.
    assert g.observe(errored_write) is None
    # The same errored write id repeated should NOT be treated as progress, so
    # after enough repeats a detector trips.  The signature is constant (failed
    # tool, same name+input), so repeated_failure (K=3) trips before
    # no_progress (N=6): turns 2/3/4 accumulate repeat_count 1/2/3 and the
    # 4th observe (3rd non-progress) returns repeated_failure.
    last = None
    for _ in range(DEFAULT_NOPROGRESS_N + 1):
        last = g.observe(errored_write)
    assert last is not None
    assert getattr(last, "kind", "") == "repeated_failure"

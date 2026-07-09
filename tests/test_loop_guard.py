"""Fix H: provider-agnostic loop detector (repeated-failure + no-progress)."""

from __future__ import annotations

from imp.agents.loop_guard import (
    DEFAULT_NOPROGRESS_N,
    DEFAULT_REPEAT_K,
    LoopGuard,
    ToolOutcome,
    TurnRecord,
    _result_preview,
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


def _read_path(path: str, tool_use_id: str) -> TurnRecord:
    return TurnRecord(
        assistant_text="", has_complete_step=False,
        tools=[ToolOutcome(name="read_file", tool_use_id=tool_use_id,
                           input={"path": path}, is_error=False)])


def test_distinct_novel_reads_are_progress() -> None:
    # Reading a NEW target each turn is information acquisition = progress, so a
    # long read-heavy exploration (e.g. a code reviewer) must NOT trip. This is
    # the regression that paused 2026-06-04-rim-sloe-timber at code-reviewer.
    g = LoopGuard()
    for i in range(DEFAULT_NOPROGRESS_N + DEFAULT_REPEAT_K + 5):
        assert g.observe(_read_turn(i)) is None


def test_rotating_seen_reads_trip_no_progress() -> None:
    # Re-reading already-seen targets yields no new information. Rotating among
    # them keeps each turn's signature distinct (so repeated_failure stays
    # below K), but no_progress accumulates and trips.
    g = LoopGuard()
    assert g.observe(_read_path("a.py", "r-a0")) is None   # novel -> progress
    assert g.observe(_read_path("b.py", "r-b0")) is None   # novel -> progress
    last = None
    for i in range(DEFAULT_NOPROGRESS_N):
        path = "a.py" if i % 2 == 0 else "b.py"            # both already seen
        last = g.observe(_read_path(path, f"r-rot{i}"))
    assert last is not None
    assert getattr(last, "kind", "") == "no_progress"


def test_repeated_identical_read_trips_repeated_failure() -> None:
    # Reading the SAME target with identical input every turn is a tight loop:
    # the signature is constant, so repeated_failure (K) trips first.
    g = LoopGuard()
    assert g.observe(_read_path("a.py", "r-a0")) is None    # novel -> progress
    last = None
    for i in range(DEFAULT_REPEAT_K + 2):
        last = g.observe(_read_path("a.py", f"r-dup{i}"))   # seen, identical sig
    assert last is not None
    assert getattr(last, "kind", "") == "repeated_failure"


def test_errored_read_is_not_progress() -> None:
    # A read whose RESULT errored acquired no information, so it is not progress
    # even for a novel target.
    g = LoopGuard()
    last = None
    for i in range(DEFAULT_NOPROGRESS_N):
        last = g.observe(TurnRecord(
            assistant_text=f"try {i}", has_complete_step=False,
            tools=[ToolOutcome(name="read_file", tool_use_id=f"e{i}",
                               input={"path": f"f{i}.py"}, is_error=True)]))
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


def test_read_file_window_slide_is_not_progress() -> None:
    # read_file gained offset/limit: different windows of the SAME file must
    # not each count as novel information (loop_guard.py's own comment pins
    # this — the key normalizes to path).
    def turn(offset: int) -> TurnRecord:
        return TurnRecord(
            assistant_text="", has_complete_step=False,
            tools=[ToolOutcome(name="read_file", tool_use_id=f"r{offset}",
                               input={"path": "big.txt", "offset": offset,
                                      "limit": 50},
                               is_error=False)])

    g = LoopGuard()
    assert g.observe(turn(1)) is None  # novel path -> progress
    last = None
    for i in range(2, DEFAULT_NOPROGRESS_N + 2):
        last = g.observe(turn(i))      # sliding window; path already seen
        if last is not None:
            break
    assert last is not None
    assert getattr(last, "kind", "") == "no_progress"


def test_grep_presentation_knob_rotation_is_not_progress() -> None:
    # grep gained output_mode/head_limit/before/after/context (tool-ergonomics
    # spec): rotating presentation knobs over the SAME search must not each
    # count as novel information — the analog of read_file's window slide.
    # Identity fields (pattern/path/glob/case_insensitive) stay in the key.
    def turn(n: int) -> TurnRecord:
        return TurnRecord(
            assistant_text="", has_complete_step=False,
            tools=[ToolOutcome(name="grep", tool_use_id=f"g{n}",
                               input={"pattern": "needle", "path": ".",
                                      "head_limit": n,
                                      "context": n % 3,
                                      "output_mode": "content"},
                               is_error=False)])

    g = LoopGuard()
    assert g.observe(turn(1)) is None  # novel search -> progress
    last = None
    for i in range(2, DEFAULT_NOPROGRESS_N + 2):
        last = g.observe(turn(i))      # same search, rotated knobs
        if last is not None:
            break
    assert last is not None
    assert getattr(last, "kind", "") == "no_progress"


def test_grep_different_search_is_progress() -> None:
    # The identity fields must SURVIVE normalization: a different pattern,
    # path, glob, or case_insensitive is a genuinely different search.
    def turn(n: int, **inp: object) -> TurnRecord:
        return TurnRecord(
            assistant_text="", has_complete_step=False,
            tools=[ToolOutcome(name="grep", tool_use_id=f"g{n}",
                               input=inp, is_error=False)])

    g = LoopGuard()
    assert g.observe(turn(1, pattern="a")) is None
    assert g.observe(turn(2, pattern="b")) is None
    assert g.observe(turn(3, pattern="a", glob="*.py")) is None
    assert g.observe(turn(4, pattern="a", case_insensitive=True)) is None
    assert g.observe(turn(5, pattern="a", path="src")) is None


def _failed_tool_turn(name: str, preview: str) -> TurnRecord:
    return TurnRecord(
        assistant_text="", has_complete_step=False,
        tools=[ToolOutcome(name=name, tool_use_id="", input={"path": "x"},
                           is_error=True, result_preview=preview)])


def test_result_preview_flattens_list_and_clips_to_first_line() -> None:
    assert _result_preview(None) == ""
    assert _result_preview("first\nsecond") == "first"
    assert _result_preview([{"type": "text", "text": "hello\nworld"},
                            {"type": "image"}]) == "hello"
    assert _result_preview("x" * 2000) == "x" * 1000


def test_summary_failed_tool_includes_preview_and_bare_when_empty() -> None:
    g = LoopGuard()
    for _ in range(DEFAULT_REPEAT_K):
        err = g.observe(_failed_tool_turn("bash", "boom: exit 2"))
    assert err is not None
    assert err.detail["full_summary"] == "failed bash: boom: exit 2"

    g2 = LoopGuard()
    for _ in range(DEFAULT_REPEAT_K):
        err2 = g2.observe(_failed_tool_turn("bash", ""))
    assert err2.detail["full_summary"] == "failed bash"


def test_summary_empty_name_falls_back_to_tool() -> None:
    g = LoopGuard()
    for _ in range(DEFAULT_REPEAT_K):
        err = g.observe(_failed_tool_turn("", "msg"))
    assert err.detail["full_summary"] == "failed tool: msg"


def test_summary_stuck_text_uses_first_non_empty_line() -> None:
    g = LoopGuard()
    for _ in range(DEFAULT_REPEAT_K):
        err = g.observe(_text_turn("\n\n  the real first line  \nmore"))
    assert err.detail["full_summary"] == "the real first line"


def test_no_progress_full_summary_is_last_turn_summary() -> None:
    g = LoopGuard()
    err = None
    for i in range(DEFAULT_NOPROGRESS_N):
        err = g.observe(_text_turn(f"distinct line {i}"))
    assert err is not None and err.kind == "no_progress"  # type: ignore[attr-defined]
    assert err.detail["full_summary"] == f"distinct line {DEFAULT_NOPROGRESS_N - 1}"


def test_summary_display_clips_to_200_with_trailing_ellipsis() -> None:
    g = LoopGuard()
    long = "z" * 500
    err = None
    for _ in range(DEFAULT_REPEAT_K):
        err = g.observe(_failed_tool_turn("bash", long))
    assert len(err.detail["full_summary"]) > 200
    line = f"repeated failure x{DEFAULT_REPEAT_K}: " in str(err)
    assert line and str(err).endswith("…")
    summary_part = str(err).split("repeated failure", 1)[1].split(": ", 1)[1]
    assert len(summary_part) <= 200 and summary_part.endswith("…")


def test_noprogress_streak_tracks_consecutive_nonprogress() -> None:
    g = LoopGuard()
    assert g.noprogress_streak == 0
    g.observe(_err_turn("API Error: 400 x"))
    assert g.noprogress_streak == 1
    g.observe(_err_turn("API Error: 400 x"))
    assert g.noprogress_streak == 2
    # Progress resets the streak.
    g.observe(TurnRecord(assistant_text="", tools=[], has_complete_step=True))
    assert g.noprogress_streak == 0


def test_interactive_guard_never_trips_no_progress() -> None:
    # Ten consecutive distinct no-progress turns (would trip no_progress at N=6
    # on a non-interactive guard) return None on every call.
    g = LoopGuard(interactive=True)
    for i in range(10):
        assert g.observe(_text_turn(f"thinking step {i}")) is None


def test_interactive_guard_never_trips_repeated_failure() -> None:
    # Ten identical error turns (would trip repeated_failure at K=3 on a
    # non-interactive guard) return None on every call.
    g = LoopGuard(interactive=True)
    for _ in range(10):
        assert g.observe(_err_turn("API Error: 400 x")) is None


def test_interactive_guard_records_no_per_turn_state() -> None:
    # A no-op observe must not mutate the read/write dedup sets or the counters.
    g = LoopGuard(interactive=True)
    g.observe(_write_turn("t-new"))
    g.observe(_text_turn("prose"))
    assert g._seen_write_ids == set()
    assert g._seen_read_keys == set()
    assert g._noprogress_count == 0
    assert g._repeat_count == 0
    assert g._last_sig is None


def test_non_interactive_guard_still_trips_no_progress() -> None:
    # Regression guard: default (interactive=False) still trips no_progress at N.
    g = LoopGuard(interactive=False)
    last = None
    for i in range(DEFAULT_NOPROGRESS_N):
        last = g.observe(_text_turn(f"thinking step {i}"))
    assert last is not None
    assert getattr(last, "kind", "") == "no_progress"


def test_non_interactive_guard_still_trips_repeated_failure() -> None:
    # Regression guard: default still trips repeated_failure at K.
    g = LoopGuard(interactive=False)
    assert g.observe(_err_turn("API Error: 400 x")) is None
    assert g.observe(_err_turn("API Error: 400 x")) is None
    err = g.observe(_err_turn("API Error: 400 x"))
    assert err is not None
    assert getattr(err, "kind", "") == "repeated_failure"

# tests/test_history_compaction.py
from __future__ import annotations

from imp.agents.tools.limits import KEEP_RECENT_TURNS
from imp.providers.history_compaction import HistoryRecord, results_to_evict


def _call(turn: int, cid: str, name: str) -> HistoryRecord:
    return HistoryRecord(turn=turn, call_id=cid, call_name=name)


def _result(turn: int, rid: str, body: str = "body") -> HistoryRecord:
    return HistoryRecord(turn=turn, result_id=rid, result_body=body)


def _conversation(n_turns: int, name: str = "read_file") -> list[HistoryRecord]:
    records: list[HistoryRecord] = []
    for t in range(n_turns):
        records.append(_call(t, f"c{t}", name))
        records.append(_result(t, f"c{t}"))
    return records


def test_warm_signal_evicts_nothing() -> None:
    assert results_to_evict(_conversation(6), cold=False, keep_recent_turns=3) == frozenset()


def test_empty_history_evicts_nothing() -> None:
    assert results_to_evict([], cold=True, keep_recent_turns=3) == frozenset()


def test_old_results_outside_window_are_evicted() -> None:
    # turns 0..5, keep_recent_turns=3 -> cutoff = 5 - 3 = 2 -> evict turns < 2
    got = results_to_evict(_conversation(6), cold=True, keep_recent_turns=3)
    assert got == frozenset({"c0", "c1"})


def test_current_turn_results_never_evicted() -> None:
    records = [_call(10, "cur", "read_file"), _result(10, "cur")]
    assert results_to_evict(records, cold=True, keep_recent_turns=3) == frozenset()


def test_recent_turns_inside_window_are_retained() -> None:
    # turns 0..5, cutoff = 2 -> turns 2,3,4,5 retained (3 recent + current)
    got = results_to_evict(_conversation(6), cold=True, keep_recent_turns=3)
    assert "c2" not in got and "c5" not in got


def test_default_window_uses_keep_recent_turns_constant() -> None:
    # max_turn = KEEP_RECENT_TURNS + 1 -> cutoff = 1 -> only turn 0 evicted
    records = _conversation(KEEP_RECENT_TURNS + 2)
    assert results_to_evict(records, cold=True) == frozenset({"c0"})

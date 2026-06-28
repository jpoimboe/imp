# tests/test_history_compaction.py
from __future__ import annotations

import pytest

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


@pytest.mark.parametrize("name", ["read_file", "grep", "glob", "bash", "bash_output"])
def test_old_compactable_result_is_evicted(name: str) -> None:
    # turns 0..5, cutoff=2 -> turns 0,1 old; compactable -> evicted
    got = results_to_evict(_conversation(6, name), cold=True, keep_recent_turns=3)
    assert got == frozenset({"c0", "c1"})


@pytest.mark.parametrize("name", ["edit_file", "sub_agent", "write_file"])
def test_old_non_compactable_result_not_evicted(name: str) -> None:
    got = results_to_evict(_conversation(6, name), cold=True, keep_recent_turns=3)
    assert got == frozenset()


def test_old_orphan_result_not_evicted() -> None:
    # 'orphan' has no matching tool-call record -> name resolves to None.
    # The later call bumps max_turn so 'orphan' (turn 0) is outside the window.
    records = [_result(0, "orphan"), _call(5, "live", "read_file")]
    assert results_to_evict(records, cold=True, keep_recent_turns=3) == frozenset()


def test_mixed_conversation_evicts_only_compactable() -> None:
    # turns 0..5, cutoff=2 -> turns 0,1 are old.
    # turn 0: compactable read_file; turn 1: non-compactable edit_file.
    records = [
        _call(0, "rf", "read_file"),
        _result(0, "rf"),
        _call(1, "ef", "edit_file"),
        _result(1, "ef"),
    ]
    for t in range(2, 6):
        records.append(_call(t, f"c{t}", "read_file"))
        records.append(_result(t, f"c{t}"))
    got = results_to_evict(records, cold=True, keep_recent_turns=3)
    assert got == frozenset({"rf"})

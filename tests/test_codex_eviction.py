from __future__ import annotations

import json

from imp.providers.codex_rollout import _canonical_tool_name, evict_rollout_lines
from imp.providers.history_compaction import EVICTION_PLACEHOLDER

PLACEHOLDER = EVICTION_PLACEHOLDER


def test_canonical_strips_mcp_imp_prefix() -> None:
    assert _canonical_tool_name("mcp__imp__read_file") == "read_file"


def test_canonical_strips_single_server_prefix() -> None:
    assert _canonical_tool_name("imp__glob") == "glob"


def test_canonical_passes_bare_name_through() -> None:
    assert _canonical_tool_name("bash") == "bash"


def test_canonical_none_is_none() -> None:
    assert _canonical_tool_name(None) is None


def _meta() -> str:
    return json.dumps({"timestamp": "t0", "type": "session_meta",
                       "payload": {"id": "thr", "cwd": "/w"}})


def _call(cid: str, name: str) -> str:
    return json.dumps({"timestamp": "t", "type": "response_item",
                       "payload": {"type": "function_call", "name": name,
                                   "arguments": "{}", "call_id": cid}})


def _out(cid: str, body: str) -> str:
    return json.dumps({"timestamp": "t", "type": "response_item",
                       "payload": {"type": "function_call_output",
                                   "call_id": cid, "output": body}})


def _output_of(line: str) -> str:
    return json.loads(line)["payload"]["output"]


def test_evicts_old_compactable_output() -> None:
    # turns: a=0 (read_file), b=1 (bash). keep_recent_turns=0 -> cutoff 1 ->
    # evict turn 0 (read_file). bash at turn 1 is the most-recent turn -> kept.
    lines = [_meta(), _call("a", "read_file"), _out("a", "BODY-A"),
             _call("b", "bash"), _out("b", "BODY-B")]
    out = evict_rollout_lines(lines, keep_recent_turns=0)
    assert _output_of(out[2]) == PLACEHOLDER
    assert _output_of(out[4]) == "BODY-B"


def test_keeps_non_compactable_output() -> None:
    lines = [_meta(), _call("a", "edit_file"), _out("a", "BODY-A"),
             _call("b", "bash"), _out("b", "BODY-B")]
    out = evict_rollout_lines(lines, keep_recent_turns=0)
    # edit_file is not compactable -> kept even though it is the oldest turn.
    assert _output_of(out[2]) == "BODY-A"


def test_strips_mcp_prefix_before_compactable_check() -> None:
    lines = [_meta(), _call("a", "mcp__imp__read_file"), _out("a", "BODY-A"),
             _call("b", "bash"), _out("b", "BODY-B")]
    out = evict_rollout_lines(lines, keep_recent_turns=0)
    # mcp__imp__read_file -> read_file (compactable) -> evicted.
    assert _output_of(out[2]) == PLACEHOLDER


def test_honors_keep_recent_window() -> None:
    # 4 read_file turns (a..d = turns 0..3). keep_recent_turns=1 -> cutoff 2 ->
    # evict turns 0,1 (a,b); keep turns 2,3 (c,d).
    lines = [_meta(),
             _call("a", "read_file"), _out("a", "A"),
             _call("b", "read_file"), _out("b", "B"),
             _call("c", "read_file"), _out("c", "C"),
             _call("d", "read_file"), _out("d", "D")]
    out = evict_rollout_lines(lines, keep_recent_turns=1)
    bodies = {json.loads(ln)["payload"]["call_id"]: _output_of(ln)
              for ln in out if '"function_call_output"' in ln}
    assert bodies == {"a": PLACEHOLDER, "b": PLACEHOLDER, "c": "C", "d": "D"}


def test_preserves_evicted_record_structure() -> None:
    lines = [_meta(), _call("a", "read_file"), _out("a", "BODY-A"),
             _call("b", "bash"), _out("b", "BODY-B")]
    out = evict_rollout_lines(lines, keep_recent_turns=0)
    rec = json.loads(out[2])
    assert rec["type"] == "response_item"
    assert rec["payload"]["type"] == "function_call_output"
    assert rec["payload"]["call_id"] == "a"
    assert rec["payload"]["output"] == PLACEHOLDER


def test_keeps_header_and_non_tool_lines_verbatim() -> None:
    event = json.dumps({"timestamp": "t", "type": "event_msg",
                        "payload": {"type": "token_count"}})
    lines = [_meta(), _call("a", "read_file"), event, _out("a", "BODY-A"),
             _call("b", "bash"), _out("b", "BODY-B")]
    out = evict_rollout_lines(lines, keep_recent_turns=0)
    assert out[0] == lines[0]           # session_meta header verbatim
    assert out[2] == lines[2]           # event_msg verbatim
    assert out[1] == lines[1]           # the function_call line verbatim
    assert len(out) == len(lines)       # ordering + count preserved


def test_returns_input_object_when_nothing_evicted() -> None:
    lines = [_meta(), _call("a", "edit_file"), _out("a", "BODY-A")]
    assert evict_rollout_lines(lines, keep_recent_turns=0) is lines


def test_keeps_malformed_line_verbatim() -> None:
    bad = "{not valid json"
    lines = [_meta(), bad, _call("a", "read_file"), _out("a", "BODY-A"),
             _call("b", "bash"), _out("b", "BODY-B")]
    out = evict_rollout_lines(lines, keep_recent_turns=0)
    assert out[1] == bad                       # malformed line untouched
    assert _output_of(out[3]) == PLACEHOLDER   # eviction still applied
    assert len(out) == len(lines)


def test_keeps_non_dict_json_line_verbatim() -> None:
    weird = "[1, 2, 3]"
    lines = [_meta(), weird, _call("a", "read_file"), _out("a", "BODY-A"),
             _call("b", "bash"), _out("b", "BODY-B")]
    out = evict_rollout_lines(lines, keep_recent_turns=0)
    assert out[1] == weird                      # non-dict JSON untouched
    assert _output_of(out[3]) == PLACEHOLDER    # eviction still applied
    assert len(out) == len(lines)


def test_parallel_calls_in_one_turn_share_turn_index() -> None:
    # turn0 single (x), turn1 PARALLEL (p1,p2), turn2 (y), turn3 (z).
    # keep_recent_turns=2 -> cutoff = max_turn(3) - 2 = 1 -> evict turn < 1 = {x}.
    # p1 and p2 share turn 1, so both are KEPT. If parallel calls were given
    # separate turns, p2 would be turn 2 and everything would shift so that p1
    # falls below the cutoff and gets evicted -> this assertion would fail.
    lines = [_meta(),
             _call("x", "read_file"), _out("x", "X"),
             _call("p1", "read_file"), _call("p2", "read_file"),
             _out("p1", "P1"), _out("p2", "P2"),
             _call("y", "read_file"), _out("y", "Y"),
             _call("z", "read_file"), _out("z", "Z")]
    out = evict_rollout_lines(lines, keep_recent_turns=2)
    bodies = {json.loads(ln)["payload"]["call_id"]: _output_of(ln)
              for ln in out if '"function_call_output"' in ln}
    assert bodies["x"] == PLACEHOLDER   # turn 0, below cutoff
    assert bodies["p1"] == "P1"          # turn 1, kept (shares turn with p2)
    assert bodies["p2"] == "P2"          # turn 1, kept
    assert bodies["y"] == "Y"
    assert bodies["z"] == "Z"


def test_orphan_output_without_call_is_kept() -> None:
    # An output with no preceding function_call (turn -1, unknown name) must
    # never be evicted.
    lines = [_meta(), _out("orphan", "ORPHAN-BODY"),
             _call("a", "read_file"), _out("a", "BODY-A")]
    out = evict_rollout_lines(lines, keep_recent_turns=0)
    assert _output_of(out[1]) == "ORPHAN-BODY"   # orphan kept

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "migrate_wfu_data",
    Path(__file__).resolve().parents[1] / "scripts" / "migrate_wfu_data.py",
)
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)


def _call(cid, ts="t1"):
    return {"id": cid, "agent_id": "a", "timestamp": ts, "type": "tool_call",
            "payload": {"name": "wait_for_user", "id": cid}}


def _result(cid, reply, ts="t2", error=False):
    return {"id": "r" + cid, "agent_id": "a", "timestamp": ts, "type": "tool_result",
            "payload": {"id": cid, "error": error,
                        "content": [{"type": "text", "text": reply}]}}


def test_resolved_gate_maps_to_awaiting_then_user_message():
    events = [_call("X"), _result("X", "yes")]
    out, n = mig.migrate_event_lines(events)
    assert n == 1
    assert out[0]["type"] == "awaiting_user_input"
    assert out[0]["payload"] == {"prompt": ""}
    assert out[0]["agent_id"] == "a" and out[0]["timestamp"] == "t1"
    assert out[1]["type"] == "user_message"
    assert out[1]["payload"] == {"role": "user", "content": [{"type": "text", "text": "yes"}]}


def test_dangling_gate_maps_to_awaiting_only():
    events = [_call("X")]
    out, n = mig.migrate_event_lines(events)
    assert n == 1
    assert [e["type"] for e in out] == ["awaiting_user_input"]


def test_non_wfu_events_untouched():
    other = {"type": "step_started", "payload": {"x": 1}}
    out, n = mig.migrate_event_lines([other])
    assert n == 0 and out == [other]


def test_find_unsafe_flags_error_and_interleaved_but_not_dangling():
    interleaving = {"type": "tool_call", "payload": {"name": "bash", "id": "B"}}
    events = [_call("X"), interleaving, _result("X", "hi"),   # interleaved -> unsafe
              _call("Y"), _result("Y", "bad", error=True),    # error -> unsafe
              _call("Z")]                                      # dangling -> safe
    assert sorted(mig.find_unsafe_events(events)) == ["X", "Y"]

import json
from pathlib import Path

from imp.evals.models import Telemetry
from imp.evals.telemetry import read_telemetry


def test_telemetry_carries_auth_mode_field() -> None:
    t = Telemetry.zero()
    # Default is None: non-codex providers omit it; consumers read None as
    # "not applicable / real spend".
    assert t.auth_mode is None

    t2 = Telemetry(
        input_tokens=1, output_tokens=2, cache_read_tokens=3,
        cache_creation_tokens=4, tool_calls=5, wall_seconds=6.0,
        auth_mode="chatgpt",
    )
    assert t2.auth_mode == "chatgpt"

    # Round-trips through to_dict/from_dict (this is the persisted shape that
    # downstream eval consumers actually read).
    d = t2.to_dict()
    assert d["auth_mode"] == "chatgpt"
    assert Telemetry.from_dict(d).auth_mode == "chatgpt"

    # Backward shape (no auth_mode key) defaults to None.
    legacy = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
              "cache_creation_tokens": 0, "tool_calls": 0, "wall_seconds": 0.0}
    assert Telemetry.from_dict(legacy).auth_mode is None


def test_read_telemetry_picks_up_auth_mode_from_llm_response(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    rows = [
        {"id": "1", "agent_id": "step:a", "timestamp": "2026-06-05T00:00:00+00:00",
         "type": "llm_response",
         "payload": {"provider": "codex", "model": "gpt-5.5",
                     "auth_mode": "chatgpt",
                     "usage": {"input_tokens": 10, "output_tokens": 5}}},
        {"id": "2", "agent_id": "step:a", "timestamp": "2026-06-05T00:00:01+00:00",
         "type": "tool_call", "payload": {"name": "edit"}},
    ]
    events.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    t = read_telemetry(events, agent_id="step:a")
    assert t.auth_mode == "chatgpt"
    assert t.tool_calls == 1


def test_read_telemetry_omits_auth_mode_for_non_codex(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    rows = [
        {"id": "1", "agent_id": "step:b", "timestamp": "2026-06-05T00:00:00+00:00",
         "type": "llm_response",
         "payload": {"provider": "openai", "model": "gpt-5.5",
                     "usage": {"input_tokens": 1, "output_tokens": 1}}},
    ]
    events.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert read_telemetry(events, agent_id="step:b").auth_mode is None

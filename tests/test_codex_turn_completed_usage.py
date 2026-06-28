from typing import Any

from imp.engine.state import InMemoryEventLog
from imp.providers.codex_runner import process_event
from tests.codex_fakes import agent_message, turn_completed


def test_turn_completed_attaches_normalized_usage_to_last_llm_response() -> None:
    """Codex emits FLAT usage keys: `cached_input_tokens` →
    `cache_read_tokens`, `reasoning_output_tokens` → reasoning bucket. Do NOT
    rely on `_normalize_usage`'s OpenAI nested-key picker — it silently records
    zeros for codex (design "Event mapping" table)."""
    elog = InMemoryEventLog()
    state: dict[str, Any] = {"pending_usage": None, "auth_mode": "chatgpt"}
    # First emit an agent_message so a llm_response exists to receive usage.
    process_event(agent_message("done"), elog=elog, agent_id="step:a",
                  state=state, provider="codex", model="gpt-5.5")
    # turn.completed carries flat usage; runner attaches it to the last
    # llm_response payload + emits a synthetic usage event for telemetry.
    process_event(
        turn_completed(input_tokens=1000, output_tokens=500,
                       cached_input_tokens=200, reasoning_output_tokens=100),
        elog=elog, agent_id="step:a", state=state,
        provider="codex", model="gpt-5.5",
    )
    llm = [e for e in elog.read() if e["type"] == "llm_response"]
    assert len(llm) == 1
    usage = llm[0]["payload"]["usage"]
    # Anthropic-style keys (read_telemetry's anthropic branch maps these), so
    # `_normalize_usage` records the right buckets for codex too:
    # input_tokens excludes cache; cache_read_input_tokens carries cached.
    assert usage["input_tokens"] == 1000 - 200  # fresh = total - cached
    assert usage["output_tokens"] == 500
    assert usage["cache_read_input_tokens"] == 200
    # Reasoning surfaces as a separate key for downstream visibility.
    assert usage.get("reasoning_output_tokens") == 100


def test_turn_completed_stashes_normalized_usage_in_proc_state() -> None:
    """The Tier A watch reads proc_state['pending_usage']; process_event must
    populate it with the SAME anthropic-shaped normalized dict it attaches to
    the llm_response (fresh = total - cached; cached -> cache_read)."""
    elog = InMemoryEventLog()
    state: dict[str, Any] = {"pending_usage": None, "auth_mode": "chatgpt"}
    process_event(agent_message("done"), elog=elog, agent_id="step:a",
                  state=state, provider="codex", model="gpt-5.5")
    process_event(
        turn_completed(input_tokens=1000, output_tokens=500,
                       cached_input_tokens=200, reasoning_output_tokens=100),
        elog=elog, agent_id="step:a", state=state,
        provider="codex", model="gpt-5.5",
    )
    assert state["pending_usage"] == {
        "input_tokens": 800,            # fresh = 1000 - 200 cached
        "output_tokens": 500,
        "cache_read_input_tokens": 200,
        "cache_creation_input_tokens": 0,
        "reasoning_output_tokens": 100,
    }

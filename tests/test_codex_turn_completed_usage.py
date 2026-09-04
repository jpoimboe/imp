from typing import Any

import pytest

from imp.engine.state import InMemoryEventLog
from imp.providers.catalog import BillingUsage, cost_usd
from imp.providers.codex_runner import process_event
from tests.codex_fakes import agent_message, turn_completed


def test_turn_completed_attaches_normalized_usage_to_last_agent_message() -> None:
    """Codex emits FLAT usage keys: `cached_input_tokens` →
    `cache_read_tokens`, `reasoning_output_tokens` → reasoning bucket. Do NOT
    rely on `_normalize_usage`'s OpenAI nested-key picker — it silently records
    zeros for codex (design "Event mapping" table)."""
    elog = InMemoryEventLog()
    state: dict[str, Any] = {"auth_mode": "chatgpt"}
    # First emit an agent_message so an agent_message exists to receive usage.
    process_event(agent_message("done"), elog=elog, agent_id="step:a",
                  state=state, provider="codex", model="gpt-5.5")
    # turn.completed carries flat usage; runner attaches it to the last
    # agent_message payload + emits a synthetic usage event for telemetry.
    process_event(
        turn_completed(input_tokens=1000, output_tokens=500,
                       cached_input_tokens=200, reasoning_output_tokens=100),
        elog=elog, agent_id="step:a", state=state,
        provider="codex", model="gpt-5.5",
    )
    llm = [e for e in elog.read() if e["type"] == "agent_message"]
    assert len(llm) == 1
    usage = llm[0]["payload"]["usage"]
    # Anthropic-style keys (read_telemetry's anthropic branch maps these), so
    # `_normalize_usage` records the right buckets for codex too:
    # input_tokens excludes cache; cache_read_input_tokens carries cached.
    assert usage["input_tokens"] == 1000 - 200  # fresh = total - cached - cache_write
    assert usage["output_tokens"] == 500
    assert usage["cache_read_input_tokens"] == 200
    # Reasoning surfaces as a separate key for downstream visibility.
    assert usage.get("reasoning_output_tokens") == 100


def _patched_usage(**turn: int) -> dict[str, Any]:
    """Drive one codex turn and return the usage the runner attached."""
    elog = InMemoryEventLog()
    state: dict[str, Any] = {"auth_mode": "chatgpt"}
    process_event(agent_message("done"), elog=elog, agent_id="step:a",
                  state=state, provider="codex", model="gpt-5.6")
    process_event(turn_completed(**turn), elog=elog, agent_id="step:a",
                  state=state, provider="codex", model="gpt-5.6")
    msgs = [e for e in elog.read() if e["type"] == "agent_message"]
    assert len(msgs) == 1
    return dict(msgs[0]["payload"]["usage"])


def _billing(usage: dict[str, Any]) -> BillingUsage:
    # Carries the already-normalized patch keys across into the priced quadruple.
    return BillingUsage(
        fresh_input=usage["input_tokens"],
        cached_input=usage["cache_read_input_tokens"],
        cache_write=usage["cache_creation_input_tokens"],
        output=usage["output_tokens"])


def test_cache_write_is_subtracted_from_fresh_input_exactly_once() -> None:
    """Both codex cache buckets nest inside `input_tokens`, so fresh is net of
    both: 1000 - 600 - 100. 400 would bill the cache-write tokens twice (once
    as fresh, once at the cache-write rate); 1000 would subtract nothing."""
    usage = _patched_usage(input_tokens=1000, output_tokens=500,
                           cached_input_tokens=600,
                           cache_write_input_tokens=100)
    assert usage["input_tokens"] == 300
    assert usage["cache_read_input_tokens"] == 600
    assert usage["cache_creation_input_tokens"] == 100
    assert usage["output_tokens"] == 500


def test_cache_write_tokens_reach_the_log_and_raise_the_priced_cost() -> None:
    """gpt-5.6 bills cache writes at 1.25x input (5.00 vs 4.00 per million), so
    moving 100 tokens out of fresh and into the cache-write bucket must cost
    MORE, not less. Both figures are hand-worked from the published rates."""
    with_write = _patched_usage(input_tokens=1000, output_tokens=500,
                                cached_input_tokens=600,
                                cache_write_input_tokens=100)
    without = _patched_usage(input_tokens=1000, output_tokens=500,
                             cached_input_tokens=600)
    # 300*4.00 + 600*0.40 + 100*5.00 + 500*20.00 = 11_940 per million
    assert cost_usd("gpt-5.6", _billing(with_write)) == pytest.approx(0.01194)
    # 400*4.00 + 600*0.40 + 0 + 500*20.00 = 11_840 per million
    assert cost_usd("gpt-5.6", _billing(without)) == pytest.approx(0.01184)
    assert cost_usd("gpt-5.6", _billing(with_write)) > cost_usd(
        "gpt-5.6", _billing(without))

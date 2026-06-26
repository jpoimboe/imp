from __future__ import annotations

from imp.engine.state import InMemoryEventLog


def test_context_fields_present_when_supplied() -> None:
    log = InMemoryEventLog()
    ev = log.append_llm_response(
        agent_id="step:p/s", provider="claude_code", model="opus-1m",
        raw={}, usage={}, stop_reason=None,
        context_window=1_000_000, context_pct=12.5, context_tokens=125_000,
    )
    assert ev["payload"]["context_window"] == 1_000_000
    assert ev["payload"]["context_pct"] == 12.5
    assert ev["payload"]["context_tokens"] == 125_000


def test_context_fields_omitted_when_absent() -> None:
    log = InMemoryEventLog()
    ev = log.append_llm_response(
        agent_id="step:p/s", provider="claude_code", model="opus",
        raw={}, usage={}, stop_reason=None,
    )
    assert "context_window" not in ev["payload"]
    assert "context_pct" not in ev["payload"]
    assert "context_tokens" not in ev["payload"]

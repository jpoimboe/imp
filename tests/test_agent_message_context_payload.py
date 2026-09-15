from __future__ import annotations

import pytest

from imp.engine.state import InMemoryEventLog


def test_context_fields_present_when_supplied() -> None:
    log = InMemoryEventLog()
    ev = log.append_agent_message(
        agent_id="step:p/s", provider="claude", model="opus-1m",
        raw={}, usage={}, stop_reason=None,
        context_window=1_000_000, context_tokens=125_000,
    )
    assert ev["payload"]["context_window"] == 1_000_000
    assert ev["payload"]["context_tokens"] == 125_000
    assert "context_pct" not in ev["payload"]


def test_context_fields_omitted_when_absent() -> None:
    log = InMemoryEventLog()
    ev = log.append_agent_message(
        agent_id="step:p/s", provider="claude", model="opus",
        raw={}, usage={}, stop_reason=None,
    )
    assert "context_window" not in ev["payload"]
    assert "context_tokens" not in ev["payload"]


def test_context_pct_keyword_is_rejected() -> None:
    log = InMemoryEventLog()
    with pytest.raises(TypeError):
        log.append_agent_message(
            agent_id="step:p/s", provider="claude", model="opus",
            raw={}, usage={}, stop_reason=None, context_pct=12.5)

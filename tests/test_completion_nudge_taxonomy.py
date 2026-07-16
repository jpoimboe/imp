from __future__ import annotations

from imp.engine.completion import build_continuation_fn
from imp.engine.feedback import COMPLETION_NUDGE
from imp.engine.state import InMemoryEventLog


def test_completion_nudge_emits_engine_message_completion() -> None:
    log = InMemoryEventLog()
    # A schema-bearing, non-interactive agent's first bare turn-end fires the
    # one-shot disambiguation nudge (no coordinator ⇒ no interject to satisfy it).
    cont = build_continuation_fn(
        event_log=log, agent_id="step:a", has_schema=True, interactive=False)
    reply, human = cont("assistant text with no complete_step")
    assert reply == COMPLETION_NUDGE
    assert human is False
    ev = next(e for e in log.read() if e["type"] == "engine_message")
    assert ev["payload"]["subtype"] == "completion"
    assert ev["payload"]["content"][0]["text"] == COMPLETION_NUDGE
    assert not [e for e in log.read() if e["type"] == "completion_nudge"]

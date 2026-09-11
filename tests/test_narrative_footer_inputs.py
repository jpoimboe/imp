"""The live footer's two producer-side inputs (spec §4): the auth mode and the
latest quota reading, retained on the builder one slot per provider."""

from __future__ import annotations

from typing import Any

from imp.cli.narrative import Carrier, NarrativeBuilder, carrier_provider
from imp.providers.quota import QuotaReading, QuotaWindow
from tests._event_fixtures import ev


def _reading(provider: str, *windows: QuotaWindow,
             plan: str | None = None) -> QuotaReading:
    return QuotaReading(provider=provider, plan=plan, windows=windows,
                        raw={"carrier": provider})


def _codex_step(b: NarrativeBuilder) -> None:
    b.add(ev("2", "step_started", step="s", kind="agent", phase="p",
             model="gpt-5.6-terra", provider="codex"))


def _message(eid: str, agent_id: str, provider: str,
             **payload: object) -> dict[str, Any]:
    return ev(eid, "agent_message", agent_id=agent_id, provider=provider,
              model="m", stop_reason="end_turn",
              raw={"content": [{"type": "text", "text": "hi"}]}, usage={},
              **payload)


def test_the_builder_holds_one_quota_reading_per_provider() -> None:
    b = NarrativeBuilder()
    _codex_step(b)
    assert b.quota_for("codex") is None                # nothing recorded yet

    codex = _reading("codex", QuotaWindow("5h", 12.0, 1788403732), plan="plus")
    claude = _reading("claude_code", QuotaWindow("7d", 7.0, None))
    b.add(ev("q1", "quota_reading", agent_id="step:p/s", **codex.to_payload()))
    b.add(ev("q2", "quota_reading", agent_id="step:p/s", **claude.to_payload()))
    assert b.quota_for("codex") == codex                # one carrier never masks
    assert b.quota_for("claude_code") == claude         # the other
    assert b.quota_for(None) is None

    newer = _reading("codex", QuotaWindow("5h", 20.0, 1788403732), plan="plus")
    b.add(ev("q3", "quota_reading", agent_id="step:p/s", **newer.to_payload()))
    assert b.quota_for("codex") == newer                # latest wins


def test_a_retired_subagents_reading_still_reaches_the_top_level_builder() -> None:
    b = NarrativeBuilder()
    _codex_step(b)
    b.add(ev("t1", "tool_call", agent_id="step:p/s", id="blk-t1", id_ns="block",
             name="sub_agent", input={"subagent_name": "coder"}))
    b.add(ev("done", "tool_result", agent_id="step:p/s", id="blk-t1", id_ns="block",
             content="done", error=False))
    step = b.tree.root.children[0].children[0]
    children_before = list(step.children)
    reading = _reading("codex", QuotaWindow("5h", 44.0, None))
    b.add(ev("q1", "quota_reading", agent_id="sub:t1", **reading.to_payload()))

    assert b.quota_for("codex") == reading
    assert step.children == children_before


def test_the_builder_holds_one_auth_mode_per_provider() -> None:
    b = NarrativeBuilder()
    assert b.auth_mode_for("claude_code") is None
    b.add(ev("2", "step_started", step="s", kind="agent", phase="p",
             model="claude-opus-5", provider="claude_code"))
    b.add(_message("3", "step:p/s", "claude_code", auth_mode="subscription"))
    b.add(_message("4", "step:p/s", "anthropic"))

    assert b.auth_mode_for("claude_code") == "subscription"
    assert b.auth_mode_for("anthropic") is None    # the API providers state none
    assert b.auth_mode_for(None) is None


def test_a_subagents_turn_does_not_erase_its_providers_auth_mode() -> None:
    # A dispatched agent runs the same provider and states no auth_mode; the
    # step's own reported mode must survive its turns.
    b = NarrativeBuilder()
    _codex_step(b)
    b.add(_message("3", "step:p/s", "codex", auth_mode="apikey"))
    b.add(ev("t1", "tool_call", agent_id="step:p/s", id="blk-t1",
             name="sub_agent", input={"subagent_name": "coder"}))
    b.add(_message("r1", "sub:t1", "codex"))

    assert b.auth_mode_for("codex") == "apikey"


def test_a_retained_reading_is_still_a_raw_fallback_line() -> None:
    # Retention is not a handler: the event keeps its bookkeeping rendering.
    b = NarrativeBuilder()
    _codex_step(b)
    b.add(ev("q1", "quota_reading", agent_id="step:p/s",
             **_reading("codex", QuotaWindow("5h", 12.0, None)).to_payload()))

    step = b.tree.root.children[0].children[0]
    assert [getattr(c, "event_type", None) for c in step.children] == ["quota_reading"]


def test_carrier_provider_names_only_step_level_agent_messages() -> None:
    assert carrier_provider(_message("3", "step:p/s", "codex")) == "codex"
    assert carrier_provider(_message("r1", "sub:t1", "codex")) is None
    assert carrier_provider(ev("q1", "quota_reading", agent_id="step:p/s",
                               provider="codex")) is None
    assert carrier_provider(ev("3", "agent_message", agent_id="step:p/s")) is None


def test_the_carrier_pairs_a_provider_with_its_own_mode_and_reading() -> None:
    b = NarrativeBuilder()
    assert b.carrier_for(None) is None            # no turn has named a carrier yet
    reading = _reading("claude_code", QuotaWindow("5h", 62.0, None))
    b.add(ev("2", "step_started", step="s", kind="agent", phase="p",
             model="claude-opus-5", provider="claude_code"))
    b.add(_message("3", "step:p/s", "claude_code", auth_mode="subscription"))
    b.add(ev("q1", "quota_reading", agent_id="step:p/s", **reading.to_payload()))

    # A provider-less step (chrome, or a drained stack) falls back to that turn.
    assert b.carrier_for(None) == Carrier(provider="claude_code",
                                          auth_mode="subscription",
                                          quota=reading)
    # A named provider still wins, and never borrows the other's mode.
    b.add(_message("4", "step:p/s", "codex", auth_mode="apikey"))
    assert b.carrier_for("claude_code") == Carrier(provider="claude_code",
                                                   auth_mode="subscription",
                                                   quota=reading)
    assert b.carrier_for("anthropic") == Carrier(provider="anthropic",
                                                 auth_mode=None, quota=None)
    # Latest step-level turn wins the fallback slot.
    assert b.carrier_for(None) == Carrier(provider="codex", auth_mode="apikey",
                                          quota=None)


def test_a_subagent_turn_does_not_set_the_last_known_provider() -> None:
    b = NarrativeBuilder()
    _codex_step(b)
    b.add(_message("3", "step:p/s", "codex", auth_mode="apikey"))
    b.add(ev("t1", "tool_call", agent_id="step:p/s", id="blk-t1",
             name="sub_agent", input={"subagent_name": "coder"}))
    b.add(_message("r1", "sub:t1", "anthropic"))

    assert b.carrier_for(None) == Carrier(provider="codex", auth_mode="apikey",
                                          quota=None)

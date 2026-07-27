"""The live view and `imp log` agree on provider-error prose: a tagged node is
withheld until its turn resolves, so the live cursor can only paint prose that the
error will not delete."""
from __future__ import annotations

import io
from typing import Any

from imp.cli import narrative as nv
from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.cli.render_text import render_tree

PROSE = "Failed to authenticate. API Error: 401 OAuth access token has been revoked."
AID = "step:p/s"


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def _ev(eid: str, etype: str, agent_id: str | None = None, **payload: Any) -> dict[str, Any]:
    return {"id": eid, "agent_id": agent_id, "type": etype, "payload": payload}


def _msg(eid: str, body: str, *, tagged: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "provider": "claude_code", "model": "m", "usage": {}, "stop_reason": None,
        "raw": {"content": [{"type": "text", "text": body}]}}
    if tagged:
        payload["provider_error"] = True
    return {"id": eid, "agent_id": AID, "type": "agent_message", "payload": payload}


def _step(*content: dict[str, Any]) -> list[dict[str, Any]]:
    return [_ev("q0", "quest_created", quest_id=1, title="x"),
            _ev("s1", "step_started", step="s", kind="agent", phase="p",
                model="m", provider="claude_code"), *content]


def _live(events: list[dict[str, Any]]) -> list[str]:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    for e in events:
        drv.on_event(e)
    drv.finish()
    return out.getvalue().splitlines()


def _log(events: list[dict[str, Any]]) -> list[str]:
    return list(render_tree(nv.build_tree(events), full=False, color=False, width=80))


def test_live_never_paints_prose_the_error_drops_and_matches_the_log() -> None:
    # No `message` on the retry: this test's subject is the node drop. The
    # breadcrumb's own copy of the text is pinned in Task 5.
    events = _step(
        _msg("a1", PROSE, tagged=True),
        _ev("q1", "query_result", agent_id=AID, usage={}, total_cost_usd=0.0),
        _ev("e1", "error", agent_id=AID, category="auth", kind="http_401",
            message="claude CLI: 401", detail={"last_agent_text": PROSE}),
        _ev("r1", "agent_retry", category="auth", attempt=2, backoff_seconds=2.0))
    body = _live(events)
    assert not any(PROSE in ln for ln in body)
    assert any("[auth] retry 2 (2.0s)" in ln for ln in body)
    assert body == _log(events)


def test_live_holds_a_tagged_node_over_a_pass_through_row_then_paints_on_release() -> None:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    for e in _step(_msg("a1", PROSE, tagged=True),
                   _ev("q1", "query_result", agent_id=AID, usage={},
                       total_cost_usd=0.0)):
        drv.on_event(e)
    assert not any(PROSE in ln for ln in out.getvalue().splitlines())

    # The first non-pass-through event resolves the tag and the cursor advances.
    drv.on_event(_ev("t1", "tool_call", agent_id=AID, id="b1", name="bash",
                     input={"cmd": "x"}))
    drv.on_event(_ev("t2", "tool_result", agent_id=AID, id="b1", content="ok",
                     error=False))
    drv.finish()
    assert any(PROSE in ln for ln in out.getvalue().splitlines())


def test_released_prose_survives_a_later_error_in_both_views() -> None:
    events = _step(
        _msg("a1", PROSE, tagged=True),
        _ev("t1", "tool_call", agent_id=AID, id="b1", name="bash", input={"cmd": "x"}),
        _ev("t2", "tool_result", agent_id=AID, id="b1", content="ok", error=False),
        _ev("e1", "error", agent_id=AID, category="auth", kind="http_401",
            message="claude CLI: 401", detail={"last_agent_text": PROSE}),
        _ev("r1", "agent_retry", category="auth", attempt=1, backoff_seconds=1.0))
    body = _live(events)
    assert any(PROSE in ln for ln in body)
    assert body == _log(events)


def test_untagged_prose_under_a_backoff_error_renders_the_same_in_both_views() -> None:
    # AC 12: prose that does not anchor-classify is the agent's own — it keeps its
    # immediate live emit, and `imp log` now agrees instead of deleting it.
    ordinary = "Reading the config file and planning the edits."
    events = _step(
        _msg("a1", ordinary, tagged=False),
        _ev("e1", "error", agent_id=AID, category="network", kind="terminated",
            message="claude_code: terminated", detail={"last_agent_text": ordinary}),
        _ev("r1", "agent_retry", category="network", attempt=1, backoff_seconds=1.0))
    body = _live(events)
    assert any(ordinary in ln for ln in body)
    assert body == _log(events)


def test_the_breadcrumb_message_renders_identically_in_both_views() -> None:
    events = _step(
        _msg("a1", PROSE, tagged=True),
        _ev("q1", "query_result", agent_id=AID, usage={}, total_cost_usd=0.0),
        _ev("e1", "error", agent_id=AID, category="auth", kind="http_401",
            message="claude CLI: 401", detail={"last_agent_text": PROSE}),
        _ev("r1", "agent_retry", category="auth", attempt=2, backoff_seconds=2.0,
            message=PROSE))
    body = _live(events)
    assert body == _log(events)
    assert any(f"[auth] retry 2 (2.0s) — {PROSE}" in ln for ln in body)
    # The text survives exactly once, as engine chrome — never also as agent prose.
    assert sum(PROSE in ln for ln in body) == 1

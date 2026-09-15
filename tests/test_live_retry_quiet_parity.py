"""A lean-hidden retry node must leave no trace in the live scrollback: live and
`imp log` agree line for line across it, blank lines included."""
from __future__ import annotations

import io
from typing import Any

from imp.cli import narrative as nv
from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.cli.render_text import render_tree


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def _ev(eid: str, etype: str, agent_id: str | None = None,
        **payload: Any) -> dict[str, Any]:
    return {"id": eid, "agent_id": agent_id, "type": etype, "payload": payload}


EVENTS: list[dict[str, Any]] = [
    _ev("q0", "quest_created", quest_id=1, title="x"),
    _ev("e1", "error", category="auth", kind="http_401", message="claude CLI: 401"),
    _ev("r1", "agent_retry", category="auth", attempt=1, backoff_seconds=1.0),
    {"id": "a1", "agent_id": None, "type": "agent_message",
     "payload": {"provider": "claude", "model": "m", "usage": {},
                 "stop_reason": None,
                 "raw": {"content": [{"type": "text", "text": "back to work"}]}}},
]


def _live(events: list[dict[str, Any]]) -> list[str]:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    for e in events:
        drv.on_event(e)
    drv.finish()
    return out.getvalue().splitlines()


def test_live_scrollback_matches_the_lean_log_across_a_quiet_retry() -> None:
    body = _live(EVENTS)
    assert body == ["• back to work"]      # no leading blank from the hidden node
    assert body == list(render_tree(nv.build_tree(EVENTS), full=False,
                                    color=False, width=80))

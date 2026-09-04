"""The login-expiry advisory on screen: one unwrapped line, tallied in the
end-of-run summary, byte-identical live and in `imp log` replay."""
from __future__ import annotations

import io
from typing import Any

from imp.cli import narrative as nv
from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.cli.render_text import render_tree

MESSAGE = "Claude login expires in 2 days — run `claude` then `/login` to renew"

EVENTS: list[dict[str, Any]] = [
    {"id": "c", "agent_id": None, "type": "quest_created",
     "payload": {"id": "abc123", "title": "x"}},
    {"id": "w1", "agent_id": None, "type": "warning",
     "payload": {"source": "claude_login_expiry", "message": MESSAGE,
                 "detail": {}}},
    {"id": "m", "agent_id": None, "type": "quest_merged",
     "payload": {"base": "master"}},
]
# quest_merged is deliberately suppressed in the live path (main.py owns that
# line), so it is excluded from the parity comparison.
PARITY_EVENTS = [e for e in EVENTS if e["type"] != "quest_merged"]


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def _live(events: list[dict[str, Any]]) -> list[str]:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    for e in events:
        drv.on_event(e)
    drv.finish()
    return out.getvalue().splitlines()


def _log(events: list[dict[str, Any]]) -> list[str]:
    return list(render_tree(nv.build_tree(events), full=False, color=False,
                            width=80))


def test_advisory_renders_lean_as_one_unwrapped_warning_line() -> None:
    assert f"warning: {MESSAGE}" in "\n".join(_log(EVENTS))


def test_advisory_is_tallied_into_the_end_of_run_summary() -> None:
    summaries = [c for c in nv.build_tree(EVENTS).root.children
                 if isinstance(c, nv.WarningsSummaryNode)]
    assert len(summaries) == 1
    assert summaries[0].lines == [(MESSAGE, 1)]
    assert summaries[0].total == 1


def test_live_and_log_render_the_advisory_identically() -> None:
    live = _live(PARITY_EVENTS)
    assert f"warning: {MESSAGE}" in "\n".join(live)
    assert live == _log(PARITY_EVENTS)

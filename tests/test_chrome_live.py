from __future__ import annotations

import io

from imp.cli import narrative as nv
from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.cli.render_text import render_tree
from tests._event_fixtures import ev


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def _events():
    return [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="scoping", kind="agent", phase="front-door",
           chrome="none", model="m", provider="anthropic", label="chatting"),
        ev("3", "llm_response", agent_id="step:front-door/scoping",
           provider="anthropic", model="m", stop_reason="end_turn", usage={},
           raw={"content": [{"type": "text", "text": "how can I help"}]}),
        ev("4", "step_completed", step="scoping", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ]


def _live_body(events) -> list[str]:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    for e in events:
        drv.on_event(e)
    drv.finish()
    return out.getvalue().splitlines()


def test_live_chrome_phase_renders_body_without_headers() -> None:
    body = "\n".join(_live_body(_events()))
    assert "how can I help" in body
    assert "front-door phase" not in body
    assert "chatting ·" not in body


def test_live_body_matches_render_tree_for_chrome_phase() -> None:
    events = _events()
    live = _live_body(events)
    batch = list(render_tree(nv.build_tree(events), full=False, color=False, width=80))
    assert [ln for ln in live if ln.strip()] == [ln for ln in batch if ln.strip()]

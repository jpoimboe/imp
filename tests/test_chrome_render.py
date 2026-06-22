from __future__ import annotations

from imp.cli import narrative as nv
from imp.cli.render_text import chrome_suppressed, render_tree
from tests._event_fixtures import ev


def _body(events) -> list[str]:
    return list(render_tree(nv.build_tree(events), full=False, color=False, width=80))


def _events(*, chrome):
    started = dict(step="scoping", kind="agent", phase="front-door",
                   model="m", provider="anthropic", label="chatting")
    if chrome is not None:
        started["chrome"] = chrome
    return [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", **started),
        ev("3", "llm_response", agent_id="step:front-door/scoping",
           provider="anthropic", model="m", stop_reason="end_turn", usage={},
           raw={"content": [{"type": "text", "text": "how can I help"}]}),
        ev("4", "step_completed", step="scoping", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ]


def test_chrome_suppressed_predicate() -> None:
    assert chrome_suppressed(nv.StepNode(name="s", chrome="none")) is True
    assert chrome_suppressed(nv.StepNode(name="s")) is False
    assert chrome_suppressed(nv.PhaseNode(name="p", chrome="none")) is True


def test_chrome_phase_renders_body_without_headers() -> None:
    out = "\n".join(_body(_events(chrome="none")))
    assert "how can I help" in out          # conversation body survives
    assert "front-door phase" not in out    # phase header suppressed
    assert "chatting ·" not in out          # step header/label suppressed


def test_non_chrome_phase_keeps_headers() -> None:
    out = "\n".join(_body(_events(chrome=None)))
    assert "how can I help" in out
    assert "front-door phase" in out        # phase header present
    assert "chatting" in out                # step header present

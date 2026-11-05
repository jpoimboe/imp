from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest

from imp.cli import narrative as nv
from imp.cli.render_html import render_html
from imp.cli.render_markdown import render_markdown
from tests._event_fixtures import ev

Doc = Callable[..., str]


def _md(events: list[dict[str, Any]], *, full: bool = False,
        show: frozenset[str] | None = None) -> str:
    return render_markdown(nv.build_tree(events), full=full, show=show)


def _html(events: list[dict[str, Any]], *, full: bool = False,
          show: frozenset[str] | None = None) -> str:
    return render_html(_md(events, full=full, show=show))


@pytest.fixture(params=[_md, _html], ids=["md", "html"])
def doc(request: pytest.FixtureRequest) -> Doc:
    render: Doc = request.param
    return render


def _headings(document: str) -> list[str]:
    tags = re.findall(r"<h([1-6])>(.*?)</h\1>", document)
    if tags:
        return [f"{'#' * int(level)} {text}" for level, text in tags]
    return [ln for ln in document.splitlines() if re.match(r"#{1,6} ", ln)]


def _started(step: str, phase: str, *, kind: str = "script", label: str | None = None,
             phase_label: str | None = None, hidden: bool = False,
             hidden_phase: bool = False, **extra: Any) -> dict[str, Any]:
    return ev("s", "step_started", step=step, kind=kind, phase=phase, label=label,
              phase_label=phase_label, show_label=not hidden,
              phase_show_label=not hidden_phase, **extra)


def _completed(step: str, *, verb: str = "continue", kind: str = "script") -> dict[str, Any]:
    return ev("c", "step_completed", step=step, kind=kind, action={"verb": verb},
              var_mutations={})


def _said(step: str, phase: str, text: str) -> dict[str, Any]:
    return ev("m", "agent_message", agent_id=f"step:{phase}/{step}", provider="anthropic",
              model="m", stop_reason="end_turn", usage={},
              raw={"content": [{"type": "text", "text": text}]})


_TITLE = ev("q", "quest_created", quest_id=1, title="t")


def test_unlabeled_phase_heading_is_its_name_and_the_word_phase(doc: Doc) -> None:
    out = doc([
        _TITLE,
        _started("write", "edit", kind="agent", label="writing"),
        _completed("write", kind="agent"),
    ])
    assert _headings(out) == ["# t", "## edit phase", "### writing"]


def test_conversation_resumed_after_the_upgrade_keeps_its_first_headings(doc: Doc) -> None:
    out = doc([
        _TITLE,
        _started("threshold", "threshold", kind="agent", hidden=True, hidden_phase=True),
        _said("threshold", "threshold", "what shall we build"),
        ev("p", "workflow_paused", reason="awaiting_user"),
        ev("r", "workflow_resumed"),
        _started("threshold", "threshold", kind="agent", label="chatting",
                 phase_label="scoping phase", hidden=True, hidden_phase=True,
                 resumed=True, restarted=False),
        _said("threshold", "threshold", "a parser, please"),
        _completed("threshold", kind="agent"),
    ])
    assert _headings(out) == ["# t", "## threshold phase", "### threshold"]
    assert "a parser, please" in out
    assert "chatting" not in out
    assert "scoping phase" not in out

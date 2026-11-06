"""engine_message/undelivered_message: a lean warning, a labeled correction, no WAITING drop."""
from __future__ import annotations

import io
from typing import Any

import pytest

from imp.cli import narrative as nv
from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.cli.render_markdown import render_markdown
from imp.cli.render_text import LOG_CATEGORIES, render_tree

AGENT = "step:build/plan"
WARNING = "agent's message may not have reached you, asked it to send it again"
WARNING_LINE = f"warning: {WARNING}"
MD_WARNING_LINE = f"> **warning:** {WARNING}"
STUB = "I'll wait for your answer."
RESEND = "Which of the two readings did you mean?"
CORRECTION = "Send the lost message again."


def _ev(eid: str, etype: str, agent_id: str | None = AGENT, **payload: Any) -> dict[str, Any]:
    return {"id": eid, "agent_id": agent_id, "type": etype, "payload": payload}


def _reply(eid: str, text: str) -> dict[str, Any]:
    return _ev(eid, "agent_message", raw={"content": [{"type": "text", "text": text}]}, usage={})


def _engine(eid: str, subtype: str, text: str = CORRECTION) -> dict[str, Any]:
    return _ev(eid, "engine_message", role="user", subtype=subtype,
               content=[{"type": "text", "text": text}])


STEP = _ev("s", "step_started", agent_id=None, step="plan", kind="agent", phase="build",
           model="m", provider="anthropic")


def _firing_stop(prefix: str) -> list[dict[str, Any]]:
    return [
        _reply(f"{prefix}-stub", STUB),
        _engine(f"{prefix}-nudge", "undelivered_message"),
        _engine(f"{prefix}-cont", "continuation"),
        _reply(f"{prefix}-resend", RESEND),
    ]


FIRING = [STEP, *_firing_stop("a")]
MERGED = _ev("m", "quest_merged", agent_id=None, base="master")


def _log(events: list[dict[str, Any]], *, full: bool = False,
         show: frozenset[str] | None = None) -> list[str]:
    return list(render_tree(nv.build_tree(events), full=full, show=show, color=False, width=80))


def _live(events: list[dict[str, Any]], cats: frozenset[str]) -> list[str]:
    out = io.StringIO()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out), cats=cats)
    for e in events:
        drv.on_event(e)
    drv.finish()
    return out.getvalue().splitlines()


def _md(events: list[dict[str, Any]], *, full: bool = False,
        show: frozenset[str] | None = None) -> str:
    return render_markdown(nv.build_tree(events), full=full, show=show)


def test_lean_text_has_one_warning_between_stub_and_resend() -> None:
    lines = _log(FIRING)
    assert lines.count(WARNING_LINE) == 1
    assert lines[-5:] == [f"• {STUB}", "", WARNING_LINE, "", f"• {RESEND}"]


def test_lean_markdown_has_one_warning_between_stub_and_resend() -> None:
    lines = _md(FIRING).split("\n")
    assert lines.count(MD_WARNING_LINE) == 1
    assert lines.index(STUB) < lines.index(MD_WARNING_LINE) < lines.index(RESEND)


@pytest.mark.parametrize("cats", [frozenset(), frozenset({"prompts"}), frozenset(LOG_CATEGORIES)])
def test_live_matches_log(cats: frozenset[str]) -> None:
    live = _live(FIRING, cats)
    assert WARNING_LINE in live
    assert live == _log(FIRING, show=cats)


def test_each_firing_counts_in_the_run_warning_total() -> None:
    assert "1 warning this run" in _log([*FIRING, MERGED])
    twice = [*FIRING, *_firing_stop("b"), MERGED]
    assert "2 warnings this run" in _log(twice)


def test_lean_views_hide_the_correction() -> None:
    assert CORRECTION not in "\n".join(_log(FIRING))
    assert CORRECTION not in _md(FIRING)


def test_bare_waiting_after_the_correction_is_rendered() -> None:
    events = [STEP, _reply("a", STUB), _engine("b", "undelivered_message"),
              _engine("c", "continuation"), _reply("d", "WAITING")]
    assert "• WAITING" in _log(events)


@pytest.mark.parametrize("subtype", ["system_prompt", "agent_prompt", "continuation", "completion"])
def test_other_subtypes_add_no_warning(subtype: str) -> None:
    events = [STEP, _reply("a", STUB), _engine("b", subtype), _reply("c", RESEND), MERGED]
    assert not any("warning" in line for line in _log(events))
    assert "warning" not in _md(events)


@pytest.mark.parametrize("view", [{"full": True}, {"show": frozenset({"prompts"})}])
def test_text_views_label_the_correction(view: dict[str, Any]) -> None:
    lines = _log(FIRING, **view)
    assert lines.count("undelivered message nudge:") == 1
    assert lines[lines.index("undelivered message nudge:") + 1] == f"  {CORRECTION}"
    assert "undelivered_message:" not in lines
    assert lines.index(WARNING_LINE) < lines.index("undelivered message nudge:")


def test_markdown_folds_the_labeled_correction_when_prompts_shown() -> None:
    out = _md(FIRING, show=frozenset({"prompts"}))
    assert f"<summary>undelivered message nudge</summary>\n\n```\n{CORRECTION}\n```" in out


def test_markdown_full_inlines_the_labeled_correction() -> None:
    assert f"**undelivered message nudge**\n\n```\n{CORRECTION}\n```" in _md(FIRING, full=True)

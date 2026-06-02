"""Prompt template rendering: 3 namespaces (change/events/vars)."""

from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.prompts import PromptRenderError, build_context, render_prompt
from imp.engine.state import EventLog, JsonlEventLog


def _log(path: Path, events: list[tuple[str, dict]]) -> EventLog:
    log = JsonlEventLog(path)
    for t, p in events:
        log.append(agent_id=None, type=t, payload=p)
    return log


def test_renders_change_namespace(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(
        change_slug="my-change", change_title="My Change",
        event_log=log,
    )
    assert render_prompt(
        "Hello {{ change.title }} ({{ change.slug }})", ctx,
    ) == "Hello My Change (my-change)"


def test_renders_events_namespace(tmp_path: Path) -> None:
    log = _log(
        tmp_path / "events.jsonl",
        [
            ("step_started", {"step": "spec-writer"}),
            ("agent_output", {"status": "Success", "summary": "hi"}),
            ("step_completed", {"step": "spec-writer", "action": "continue"}),
        ],
    )
    ctx = build_context(
        change_slug="t", change_title="t",
        event_log=log,
    )
    assert render_prompt(
        "Prev: {{ events['spec-writer'].summary }}", ctx,
    ) == "Prev: hi"


def test_events_missing_step_renders_empty(tmp_path: Path) -> None:
    ctx = build_context(
        change_slug="t", change_title="t",
        event_log=_log(tmp_path / "events.jsonl", []),
    )
    assert render_prompt("{{ events['nope'].field }}", ctx) == ""


def test_undefined_top_level_raises(tmp_path: Path) -> None:
    ctx = build_context(
        change_slug="t", change_title="t",
        event_log=_log(tmp_path / "events.jsonl", []),
    )
    with pytest.raises(PromptRenderError):
        render_prompt("{{ bogus.thing }}", ctx)


def test_renders_vars_namespace(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(change_slug="t", change_title="t", event_log=log,
                        vars={"fast_path": True})
    assert render_prompt("{{ vars.fast_path }}", ctx) == "True"
    assert render_prompt("{% if vars.fast_path %}fast{% endif %}", ctx) == "fast"


def test_vars_typo_raises_strict_within(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(change_slug="t", change_title="t", event_log=log,
                        vars={"fast_path": False})
    with pytest.raises(PromptRenderError):
        render_prompt("{{ vars.fastpath }}", ctx)


def test_vars_default_empty_when_omitted(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(change_slug="t", change_title="t", event_log=log)
    # An unknown var still raises (strict-within over an empty map).
    with pytest.raises(PromptRenderError):
        render_prompt("{{ vars.anything }}", ctx)

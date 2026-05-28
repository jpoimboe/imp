"""Prompt template rendering: 3 namespaces (events/artifacts/change)."""

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
        change_id=42, change_title="My Change",
        artifacts_dir=tmp_path / "art", event_log=log,
    )
    assert render_prompt(
        "Hello {{ change.title }} (#{{ change.id }})", ctx,
    ) == "Hello My Change (#42)"


def test_renders_artifacts_namespace(tmp_path: Path) -> None:
    art = tmp_path / "art"
    art.mkdir()
    (art / "spec.md").write_text("the spec body\n")
    ctx = build_context(
        change_id=1, change_title="t", artifacts_dir=art,
        event_log=_log(tmp_path / "events.jsonl", []),
    )
    assert render_prompt("Spec:\n{{ artifacts.spec }}", ctx) == \
           "Spec:\nthe spec body\n"


def test_artifacts_missing_renders_empty(tmp_path: Path) -> None:
    art = tmp_path / "art"
    art.mkdir()
    ctx = build_context(
        change_id=1, change_title="t", artifacts_dir=art,
        event_log=_log(tmp_path / "events.jsonl", []),
    )
    assert render_prompt("Spec: '{{ artifacts.missing }}'", ctx) == "Spec: ''"


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
        change_id=1, change_title="t", artifacts_dir=tmp_path / "art",
        event_log=log,
    )
    assert render_prompt(
        "Prev: {{ events['spec-writer'].summary }}", ctx,
    ) == "Prev: hi"


def test_events_missing_step_renders_empty(tmp_path: Path) -> None:
    ctx = build_context(
        change_id=1, change_title="t", artifacts_dir=tmp_path / "art",
        event_log=_log(tmp_path / "events.jsonl", []),
    )
    assert render_prompt("{{ events['nope'].field }}", ctx) == ""


def test_undefined_top_level_raises(tmp_path: Path) -> None:
    ctx = build_context(
        change_id=1, change_title="t", artifacts_dir=tmp_path / "art",
        event_log=_log(tmp_path / "events.jsonl", []),
    )
    with pytest.raises(PromptRenderError):
        render_prompt("{{ bogus.thing }}", ctx)

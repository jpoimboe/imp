"""Tests for render_prompt / build_context (4 namespaces: change, events, vars, project)."""

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


def test_implementer_prompt_gates_tdd_on_fast_path(tmp_path: Path) -> None:
    """The shipped implementer template must render two branches: fast_path
    waives tests; else loads TDD. StrictUndefined means both renders pass
    fast_path explicitly (there is no undefined->else fallback)."""
    from imp.assets import bundled_workflow_dir
    from imp.engine.loader import load_workflow

    wf = load_workflow(bundled_workflow_dir("imp"))
    body = wf.agents["implementer"].prompt_template_source
    log = _log(tmp_path / "events.jsonl", [])

    fast = render_prompt(
        body,
        build_context(change_slug="t", change_title="t", event_log=log,
                      vars={"fast_path": True}),
    )
    slow = render_prompt(
        body,
        build_context(change_slug="t", change_title="t", event_log=log,
                      vars={"fast_path": False}),
    )
    # fast-path: no new tests, TDD skill NOT loaded; the implementer does NOT run
    # the suite or a lint gate itself — the phase-level verify gate owns that.
    assert "do **not** create or modify tests" in fast
    assert "IMP_LINT_COMMAND" not in fast
    assert "phase-level" in fast
    assert "test-driven-development" not in fast
    # standard path: TDD skill loaded; fast-path constraints absent.
    assert "test-driven-development" in slow
    assert "do **not** create or modify tests" not in slow


def test_events_render_skips_torn_trailing_line(tmp_path: Path) -> None:
    """AC7: torn trailing events-file line is skipped; prior good event still renders."""
    path = tmp_path / "events.jsonl"
    log = _log(
        path,
        [
            ("step_started", {"step": "spec-writer"}),
            ("agent_output", {"status": "Success", "summary": "good"}),
        ],
    )
    # Simulate a crash mid-append: a partial JSON line, no trailing newline.
    with path.open("a") as f:
        f.write('{"type": "agent_output", "payload": {"summa')
    ctx = build_context(change_slug="t", change_title="t", event_log=log)
    assert render_prompt(
        "Prev: {{ events['spec-writer'].summary }}", ctx,
    ) == "Prev: good"


def test_renders_project_facts(tmp_path: Path) -> None:
    ctx = build_context(
        change_slug="t", change_title="t",
        event_log=_log(tmp_path / "events.jsonl", []),
        project_facts={"test_command": "make test"}, guidance="be careful",
    )
    assert render_prompt("{{ project.test_command }}", ctx) == "make test"
    assert render_prompt("{{ project.guidance }}", ctx) == "be careful"


def test_guidance_defaults_empty(tmp_path: Path) -> None:
    ctx = build_context(
        change_slug="t", change_title="t",
        event_log=_log(tmp_path / "events.jsonl", []),
    )
    assert render_prompt("{{ project.guidance }}", ctx) == ""


def test_missing_fact_attr_raises(tmp_path: Path) -> None:
    ctx = build_context(
        change_slug="t", change_title="t",
        event_log=_log(tmp_path / "events.jsonl", []),
        project_facts={"a": "1"},
    )
    with pytest.raises(PromptRenderError):
        render_prompt("{{ project.absent }}", ctx)


def test_missing_fact_item_raises(tmp_path: Path) -> None:
    ctx = build_context(
        change_slug="t", change_title="t",
        event_log=_log(tmp_path / "events.jsonl", []),
        project_facts={"a": "1"},
    )
    with pytest.raises(PromptRenderError):
        render_prompt('{{ project["absent"] }}', ctx)


def test_default_filter_does_not_rescue_strict_namespace(tmp_path: Path) -> None:
    ctx = build_context(
        change_slug="t", change_title="t",
        event_log=_log(tmp_path / "events.jsonl", []),
        project_facts={"a": "1"},
    )
    with pytest.raises(PromptRenderError):
        render_prompt("{{ project.absent | default('x') }}", ctx)


def test_dunder_raises_attributeerror() -> None:
    from imp.agents.prompts import _ProjectNamespace
    ns = _ProjectNamespace({"a": "1", "guidance": ""})
    with pytest.raises(AttributeError):
        _ = ns.__html__

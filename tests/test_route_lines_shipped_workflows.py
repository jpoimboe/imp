from __future__ import annotations

import types as _types
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from imp.assets import bundled_workflow_dir
from imp.cli.narrative import NarrativeBuilder
from imp.cli.render_text import render_tree
from imp.engine.executor import _action_payload, _step_started_payload
from imp.engine.loader import load_workflow
from imp.engine.quest import Quest
from imp.engine.types import Goto, Rule, Step, Workflow
from tests.conftest import _seed_control

MODEL = "m"
PLAN = "plan 2/3"
_VARS = {"plan_index": 2, "plan_total": 3}
_START = "2026-06-10T00:00:00"
_END = "2026-06-10T00:00:05"


@cache
def _workflow(name: str) -> Workflow:
    return load_workflow(bundled_workflow_dir(name))


def _step(workflow: str, name: str) -> Step:
    return next(s for s in _workflow(workflow).flat_steps() if s.name == name)


def _route_rule(workflow: str, source: str, target: str, nth: int = 0) -> Rule:
    routes = [r for r in _step(workflow, source).rules
              if isinstance(r.action, Goto) and r.action.target == target]
    return routes[nth]


@pytest.fixture
def quest(tmp_path: Path) -> Quest:
    _seed_control(tmp_path)
    return Quest.create(tmp_path, title="t", quest_id="aaa111", base_branch="main")


def _ev(etype: str, timestamp: str, **payload: Any) -> dict[str, Any]:
    return {"id": etype, "agent_id": None, "type": etype, "timestamp": timestamp,
            "payload": payload}


def _started(step: Step, quest: Quest, route_label: str | None = None) -> dict[str, Any]:
    state = _types.SimpleNamespace(route_label=route_label, loop_counts={}, vars=_VARS)
    payload = _step_started_payload(step, state, quest=quest, log=quest.event_log())
    if step.kind == "agent":
        payload["model"] = MODEL
    return _ev("step_started", _START, **payload)


def _source_events(
    workflow: str, source: str, target: str, quest: Quest, nth: int = 0,
) -> list[dict[str, Any]]:
    step = _step(workflow, source)
    rule = _route_rule(workflow, source, target, nth)
    return [
        _started(step, quest),
        _ev("step_completed", _END, step=source, kind=step.kind,
            action=_action_payload(rule.action), var_mutations={}, label=rule.label),
        _ev("step_transition", _END, from_step=source, target_step=target, backward=True),
    ]


def _bounce(
    workflow: str, source: str, target: str, quest: Quest, nth: int = 0,
) -> list[dict[str, Any]]:
    rule = _route_rule(workflow, source, target, nth)
    return [*_source_events(workflow, source, target, quest, nth),
            _started(_step(workflow, target), quest, rule.label)]


def _script_completed(step: str) -> dict[str, Any]:
    return _ev("step_completed", _END, step=step, kind="script",
               action={"verb": "continue"}, var_mutations={}, label=None)


def _lines(events: list[dict[str, Any]]) -> list[str]:
    builder = NarrativeBuilder()
    for event in events:
        builder.add(event)
    rendered = render_tree(builder.tree, full=False, color=False, width=120)
    return [line.strip() for line in rendered if line.strip()]


def _route_lines(events: list[dict[str, Any]]) -> list[str]:
    return [line for line in _lines(events) if line.startswith("↩")]


@pytest.mark.parametrize(
    ("workflow", "source", "target", "nth", "line"),
    [
        ("default", "spec-reviewer", "spec-finalizer", 0,
         "↩ reviewing spec → addressing spec review feedback"),
        ("default", "plan-reviewer", "plan-writer", 0,
         "↩ reviewing plan → addressing plan review feedback"),
        ("default", "verify", "task-lead", 0,
         "↩ running tests → fixing test failures"),
        ("default", "gap-check", "plan-writer", 0,
         "↩ checking plan coverage → writing plan for coverage gaps"),
        ("default", "quest-review", "task-lead", 0,
         "↩ reviewing entire diff → addressing code review feedback"),
        ("default", "converge-verify", "deferred-sweep", 0,
         "↩ running tests → triaging slow test failures"),
        ("default", "integration-reviewer", "reconciler", 0,
         "↩ reviewing merge → addressing integration review feedback"),
        ("default", "integration-reviewer", "reconciler", 1,
         "↩ reviewing merge → integrating minor improvements"),
        ("default", "finalize", "plan-writer", 0,
         "↩ finalizing → addressing review feedback"),
        ("default", "merge-to-base", "reconciler", 0,
         "↩ merging → committing leftover changes"),
        ("default", "merge-to-base", "integrate-rebase", 0,
         "↩ merging → retrying integration"),
        ("minimal", "review", "implement", 0,
         "↩ reviewing the change → addressing review feedback"),
        ("default", "scope-code-check", "threshold", 0,
         "↩ undoing code committed during scoping"),
        ("default", "spec-written", "threshold", 0,
         "↩ writing the missing spec"),
    ],
)
def test_shipped_route_renders_its_line(
    workflow: str, source: str, target: str, nth: int, line: str, quest: Quest,
) -> None:
    assert _route_lines(_bounce(workflow, source, target, quest, nth)) == [line]


@pytest.mark.parametrize(
    ("workflow", "source", "target", "header"),
    [
        ("default", "spec-verify-commit", "spec-finalizer",
         f"spec phase · fixing the spec file · {MODEL}"),
        ("default", "plan-verify-commit", "plan-writer",
         f"planning phase · {PLAN} · writing the missing plan · {MODEL}"),
        ("default", "check-next-plan", "plan-writer",
         f"planning phase · {PLAN} · writing plan · {MODEL}"),
        ("default", "validate-memory", "reconciler",
         f"integration phase · fixing memory files · {MODEL}"),
        ("memory", "validate-memory", "memory-editor",
         f"fixing memory files · {MODEL}"),
    ],
)
def test_route_from_hidden_source_shows_only_the_target_header(
    workflow: str, source: str, target: str, header: str, quest: Quest,
) -> None:
    events = _bounce(workflow, source, target, quest)
    assert _route_lines(events) == []
    assert _lines(events)[-1] == header


def test_check_cleared_bounce_shows_only_the_gate_completion_line(quest: Quest) -> None:
    events = _bounce("default", "check-cleared", "integration-verify", quest)
    assert _route_lines(events) == []
    completed = _lines([*events, _script_completed("integration-verify")])
    assert completed[-1] == "integration phase · re-running tests · complete · 5s"


def test_hidden_unlabelled_target_adds_no_line(quest: Quest) -> None:
    head = _source_events("default", "autosquash-resolve-merge", "cleanup-merge", quest)
    events = _bounce("default", "autosquash-resolve-merge", "cleanup-merge", quest)
    assert _lines([*events, _script_completed("cleanup-merge")]) == _lines(head)


@pytest.mark.parametrize(
    ("workflow", "source", "target", "label"),
    [
        ("default", "spec-verify-commit", "spec-finalizer", "fixing the spec file"),
        ("default", "plan-verify-commit", "plan-writer", "writing the missing plan"),
        ("default", "gap-check", "plan-writer", "writing plan for coverage gaps"),
        ("default", "converge-verify", "deferred-sweep", "triaging slow test failures"),
        ("default", "validate-memory", "reconciler", "fixing memory files"),
        ("default", "check-cleared", "integration-verify", "re-running tests"),
        ("default", "merge-to-base", "reconciler", "committing leftover changes"),
        ("memory", "validate-memory", "memory-editor", "fixing memory files"),
    ],
)
def test_route_carries_its_label(workflow: str, source: str, target: str, label: str) -> None:
    assert _route_rule(workflow, source, target).label == label

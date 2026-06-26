from __future__ import annotations

from imp.agents.prompts import build_context, render_prompt
from imp.assets import bundled_workflow_dir
from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.state import JsonlEventLog
from imp.engine.types import Goto

_BASE_VARS = {
    "fast_path": False, "spec_minors_integrated": False,
    "plan_minors_integrated": False, "review_scope": "plan",
    "review_base": "",
    "change_panel_done": False, "integration_fork": "",
}


def _wf():
    return load_workflow(bundled_workflow_dir("imp"))


def _steps(wf):
    return {s.name: s for s in wf.flat_steps()}


def test_front_door_is_first_phase_with_chrome_none() -> None:
    wf = _wf()
    assert wf.phases[0].name == "front-door"
    step = wf.phases[0].steps[0]
    assert step.name == "front-door"
    assert step.kind == "agent"
    assert step.chrome == "none"          # bare-conversation render (Part 1 mechanism)
    assert step.label == "<imp>"


def test_front_door_express_routes_to_plan_writer_and_sets_fast_path() -> None:
    rules = _steps(_wf())["front-door"].rules
    m = evaluate_rules(rules, vars=_BASE_VARS,
                       output={"status": "Success", "ceremony": "express"})
    assert isinstance(m.action, Goto)
    assert m.action.target == "plan-writer"
    assert m.action.resume is False                 # cross-phase plain goto, skips spec beat
    assert m.var_mutations["fast_path"] is True


def test_front_door_full_routes_to_spec_finalizer_and_clears_fast_path() -> None:
    rules = _steps(_wf())["front-door"].rules
    m = evaluate_rules(rules, vars=_BASE_VARS,
                       output={"status": "Success", "ceremony": "full"})
    assert isinstance(m.action, Goto)
    assert m.action.target == "spec-finalizer"
    assert m.action.resume is False
    assert m.var_mutations["fast_path"] is False


def test_front_door_declares_ceremony_output() -> None:
    agent = _wf().agents["front-door"]
    field = next(f for f in agent.output_schema.fields if f.name == "ceremony")
    assert field.type == "enum"
    assert field.values == ("express", "full")
    assert field.required is True


def test_spec_finalizer_no_longer_sets_fast_path() -> None:
    # Ownership of fast_path moved to front-door; spec-finalizer's terminal rule
    # only continues to the verify gate now.
    rules = _steps(_wf())["spec-finalizer"].rules
    m = evaluate_rules(rules, vars=_BASE_VARS,
                       output={"phase": "written", "complexity": "trivial",
                               "status": "Success"})
    assert "fast_path" not in m.var_mutations


def test_front_door_agent_prose_carries_the_three_hats_and_handoff() -> None:
    body = _wf().agents["front-door"].prompt_template_source
    low = body.lower()
    for hat in ("concierge", "scout", "brainstorm"):
        assert hat in low, hat
    assert "create_and_enter_worktree" in body         # go-live tool
    assert "rename_change" in body                      # names the change
    assert "{{ change.home }}/artifacts/spec/{{ change.id }}.md" in body
    # AC10: a standing directive never to leak internal vocabulary to the user.
    assert "never surface these internal terms" in low
    assert "announce the name in prose without asking permission" in body


def test_front_door_headless_auto_go_live_branch(tmp_path) -> None:
    # front-door is the UNCONDITIONAL first step of the default workflow, so a
    # headless (--non-interactive) run must auto-go-live instead of parking on
    # wait_for_user. The non_interactive branch renders an auto-build directive
    # and a predetermined ceremony; the interactive render must not.
    body = _wf().agents["front-door"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")

    headless = render_prompt(body, build_context(
        change_title="t", event_log=log, change_id="abc",
        change_home=".imp/changes/abc", non_interactive=True,
        project_facts={}, guidance=""),
        workflow_dir=bundled_workflow_dir("imp")).lower()
    assert "headless run" in headless
    assert "the ceremony is predetermined" in headless
    assert 'ceremony="full"' in headless          # auto-build literal, unique to this branch

    interactive = render_prompt(body, build_context(
        change_title="t", event_log=log, change_id="abc",
        change_home=".imp/changes/abc", non_interactive=False,
        project_facts={}, guidance=""),
        workflow_dir=bundled_workflow_dir("imp")).lower()
    assert "the ceremony is predetermined" not in interactive
    assert 'ceremony="full"' not in interactive


def test_front_door_includes_both_brainstorm_partials() -> None:
    body = _wf().agents["front-door"].prompt_template_source
    assert '{% include "partials/spec-design-principles.md" %}' in body
    assert '{% include "partials/brainstorm-dialogue.md" %}' in body


def test_front_door_renders_full_brainstorm_depth(tmp_path) -> None:
    body = _wf().agents["front-door"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    out = render_prompt(body, build_context(
        change_title="t", event_log=log, change_id="abc",
        change_home=".imp/changes/abc", non_interactive=False,
        project_facts={}, guidance=""),
        workflow_dir=bundled_workflow_dir("imp"))
    low = out.lower()
    # dialogue conduct expanded from brainstorm-dialogue
    assert "one question at a time" in low
    assert "present it in sections" in out
    assert "build this out now" in low
    assert "final readback before committing" in low
    # design lens expanded from spec-design-principles
    assert "architecture, components, data flow, error handling, and testing" in out
    assert "do not propose unrelated refactoring" in out


def test_front_door_owns_complexity_classification() -> None:
    body = _wf().agents["front-door"].prompt_template_source
    low = body.lower()
    # front-door decides the draft's shape (thin vs four-part) and never leaks the label.
    assert "trivial" in low and "standard" in low
    assert "goal + acceptance" in low or "goal and acceptance" in low
    assert "never name the internal classification" in low


def test_front_door_prose_names_spec_finalizer_not_spec_writer(tmp_path) -> None:
    body = _wf().agents["front-door"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    out = render_prompt(body, build_context(
        change_title="t", event_log=log, change_id="abc",
        change_home=".imp/changes/abc", non_interactive=False,
        project_facts={}, guidance=""),
        workflow_dir=bundled_workflow_dir("imp"))
    assert "spec-writer" not in out
    assert "spec-finalizer" in out


def test_front_door_bare_open_keeps_concierge_and_scout_hats(tmp_path) -> None:
    # Regression guard: the moved unseeded-open prose must NOT force a build-only
    # question or a no-file-read freeze on the bare <imp:begin/> open.
    body = _wf().agents["front-door"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    out = render_prompt(body, build_context(
        change_title="t", event_log=log, change_id="abc",
        change_home=".imp/changes/abc", project_facts={}, guidance=""),
        workflow_dir=bundled_workflow_dir("imp"))
    low = out.lower()
    assert "do not read any files, do not explore the codebase" not in low
    assert "what the user wants to build" not in low
    assert "take stock of existing work" in low
    assert "their reply picks the hat" in low


def test_express_path_skips_spec_beat_full_path_keeps_it() -> None:
    wf = _wf()
    steps = _steps(wf)
    # express: front-door jumps straight to plan-writer (the spec phase is bypassed).
    express = evaluate_rules(steps["front-door"].rules, vars=_BASE_VARS,
                             output={"status": "Success", "ceremony": "express"})
    assert isinstance(express.action, Goto) and express.action.target == "plan-writer"
    # full: front-door enters spec-finalizer, the first chromed beat.
    full = evaluate_rules(steps["front-door"].rules, vars=_BASE_VARS,
                          output={"status": "Success", "ceremony": "full"})
    assert isinstance(full.action, Goto) and full.action.target == "spec-finalizer"
    # The spec phase is still the second phase, entered only on the full path.
    assert [p.name for p in wf.phases][:2] == ["front-door", "spec"]


def test_front_door_routes_inline_satisfiable_requests_to_bare_turn_end_yield() -> None:
    # A pure question or a content/text-generation ask that is not a software
    # change is answered in prose and then the agent ends its turn to yield —
    # never complete_step, which is exclusively the go-live build terminator.
    body = _wf().agents["front-door"].prompt_template_source
    low = body.lower()
    assert "text-generation ask" in low
    assert "answered in prose" in low and "end your turn to yield" in body
    assert "wait_for_user" not in body
    assert "**never** `complete_step`" in body
    # complete_step is framed as the build terminator, and express stays a build path.
    assert "go-live build terminator" in low
    assert "build *quickly*" in body
    assert "no-build exit" in low


def test_front_door_status_label_resolves_to_a_pool_member() -> None:
    from imp.agents.front_door_status import FRONT_DOOR_STATUS_PHRASES
    from imp.agents.prompts import render_label
    step = _wf().phases[0].steps[0]
    rendered = render_label(step.label, change=None, log=None)
    assert rendered in FRONT_DOOR_STATUS_PHRASES


def test_front_door_live_gate_conventions_relocated_to_partial(tmp_path) -> None:
    body = _wf().agents["front-door"].prompt_template_source
    # The inline convention list is gone from front-door's own raw body...
    # (guard on the sentence's lead-in, which — unlike the line-wrapped phrase —
    # is a contiguous string that a revert would reintroduce).
    assert "Follow the live-gate" not in body
    # ...but resolves through the included partial chain in the rendered prompt.
    log = JsonlEventLog(tmp_path / "events.jsonl")
    out = render_prompt(body, build_context(
        change_title="t", event_log=log, change_id="abc",
        change_home=".imp/changes/abc", non_interactive=False,
        project_facts={}, guidance=""),
        workflow_dir=bundled_workflow_dir("imp"))
    assert "no co-equal dangling alternative" in out

from __future__ import annotations

import os
import shutil
from pathlib import Path

from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.types import Continue, Goto, Pause


def _steps(wf):
    return {s.name: s for s in wf.flat_steps()}


# All declared workflow vars at their defaults — a realistic evaluate_rules fixture.
_BASE_VARS = {
    "fast_path": False, "spec_minors_integrated": False,
    "plan_minors_integrated": False,
    "review_scope": "plan",
    "review_base": "",
    "change_panel_done": False,
}


def test_shipped_default_workflow_loads(tmp_path: Path) -> None:
    asset = Path("src/imp/assets/workflows/imp").resolve()
    shutil.copytree(asset, tmp_path / "wf")
    wf = load_workflow(tmp_path / "wf")
    assert wf.name
    assert wf.phases
    for agent in wf.agents.values():
        assert "complete_step" not in agent.tools, (
            f"{agent.name}: complete_step is provider-injected"
        )
        assert "load_skill" not in agent.tools, (
            f"{agent.name}: load_skill is provider-injected"
        )


def test_default_agents_resolve_to_position_appropriate_tools(tmp_path: Path) -> None:
    from imp.engine.loader import BASE_CAPS
    asset = Path("src/imp/assets/workflows/imp").resolve()
    shutil.copytree(asset, tmp_path / "wf")
    wf = load_workflow(tmp_path / "wf")

    top_leaf = BASE_CAPS | {"wait_for_user", "rename_change", "create_change",
                            "create_and_enter_worktree"}
    top_dispatcher = top_leaf | {"task"}
    subagents = {
        "implementer", "quality-reviewer",
    }
    dispatchers = {"task-lead", "deferred-sweep",
                   "plan-writer", "spec-writer", "front-door"}

    for name, agent in wf.agents.items():
        assert "complete_step" not in agent.tools, name
        assert "load_skill" not in agent.tools, name
        if agent.explicit_tools:
            # Hand-scoped read-only subagents (explorer, researcher) declare
            # their own tools and are not subject to the all-by-default buckets.
            continue
        if name in subagents:
            assert agent.tools == BASE_CAPS, name
        elif name in dispatchers:
            assert agent.tools == top_dispatcher, name
        else:
            assert agent.tools == top_leaf, name


def test_spec_writer_does_not_declare_ask_user(tmp_path: Path) -> None:
    asset = Path("src/imp/assets/workflows/imp").resolve()
    shutil.copytree(asset, tmp_path / "wf")
    wf = load_workflow(tmp_path / "wf")
    spec_writer = wf.agents["spec-writer"]
    assert "ask_user" not in spec_writer.tools
    assert "complete_step" not in spec_writer.tools


def test_bundled_workflow_loads_and_producers_resume() -> None:
    from imp.assets import bundled_workflow_dir
    from imp.engine.types import Goto
    wf_dir = bundled_workflow_dir("imp")
    wf = load_workflow(wf_dir)
    by = {s.name: s for s in wf.flat_steps()}
    # producer back-edges resume their target's conversation
    assert isinstance(by["spec-verify-commit"].rules[-1].action, Goto)
    assert by["spec-verify-commit"].rules[-1].action.resume is True   # -> spec-writer
    assert by["plan-verify-commit"].rules[-1].action.resume is True   # -> plan-writer
    assert by["verify"].rules[-1].action.resume is True            # -> task-lead
    assert by["spec-reviewer"].rules[0].action.resume is True         # rework -> spec-writer
    # phase jumps clear (plain goto)
    assert by["code-reviewer"].rules[0].action.target == "task-lead"     # task_gap -> implementer
    assert by["code-reviewer"].rules[0].action.resume is False           # cross-phase plain goto
    # else: approve -> continue (deferred-sweep); plan_gap route is removed
    assert isinstance(by["code-reviewer"].rules[-1].action, Continue)
    assert by["spec-verify-commit"].kind == "script"
    # B3: the merge phase is the engine-native authoritative merge.
    assert wf.phases[-1].name == "merge"
    assert [s.name for s in wf.phases[-1].steps] == [
        "wait-for-head", "integrate-rebase", "integrator",
        "integration-verify", "integration-reviewer",
        "cleanup-merge", "autosquash-resolve-merge",
        "assert-stamped", "merge-to-base"]
    assert by["merge-to-base"].kind == "merge_and_exit_worktree"


def test_review_step_has_no_differ_from():
    from imp.assets import bundled_workflow_dir
    from imp.engine.loader import load_workflow
    wf = load_workflow(bundled_workflow_dir("imp"))
    by = {s.name: s for s in wf.flat_steps()}
    assert by["code-reviewer"].differ_from is None


def test_review_step_declares_high_tier():
    from imp.assets import bundled_workflow_dir
    from imp.engine.loader import load_workflow
    wf = load_workflow(bundled_workflow_dir("imp"))
    by = {s.name: s for s in wf.flat_steps()}
    assert by["code-reviewer"].tier == "high"


def test_default_workflow_loads() -> None:
    # Verify the default workflow loads without errors.
    from imp.assets import bundled_workflow_dir
    from imp.engine.loader import load_workflow
    wf = load_workflow(bundled_workflow_dir("imp"))
    assert wf.name == "feature"
    assert len(wf.flat_steps()) > 0


def test_spec_writer_declares_complexity_output() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    spec_writer = wf.agents["spec-writer"]
    assert "complexity" in spec_writer.output_schema.field_names()
    # complexity drives the thin-spec decision only; fast_path is owned by the
    # front-door phase now, so a renamed value no longer silently breaks routing.
    field = next(f for f in spec_writer.output_schema.fields if f.name == "complexity")
    assert field.type == "enum"
    assert field.values == ("trivial", "standard")
    assert field.required is True


def test_fast_path_var_declared_default_false() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    assert wf.vars.defaults()["fast_path"] is False


def test_minors_integrated_vars_declared_default_false() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    defaults = wf.vars.defaults()
    assert defaults["spec_minors_integrated"] is False
    assert defaults["plan_minors_integrated"] is False


def test_spec_reviewer_blocking_reworks_and_clears_flag() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["spec-reviewer"].rules
    # Critical/Major -> rework to spec-writer; clear the minors-integrated flag so a
    # real fix re-opens the one minors pass.
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "spec_minors_integrated": True},
        output={"blocking": True, "has_minor_findings": False},
    )
    assert isinstance(m.action, Goto)
    assert m.action.target == "spec-writer" and m.action.resume is True
    assert m.var_mutations["spec_minors_integrated"] is False


def test_spec_reviewer_clean_with_minors_integrates_then_owes_review() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["spec-reviewer"].rules
    # Clean + actionable minors, not yet integrated -> resume writer to integrate and
    # set the flag, so the next (clean) review terminates.
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "spec_minors_integrated": False},
        output={"blocking": False, "has_minor_findings": True},
    )
    assert m.var_mutations["spec_minors_integrated"] is True
    assert isinstance(m.action, Goto)
    assert m.action.target == "spec-writer" and m.action.resume is True


def test_spec_reviewer_verification_pass_stops_and_drops_minors() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["spec-reviewer"].rules
    # The owed verification review (minors already integrated): a clean review stops
    # and advances to plan-writer even if it surfaced fresh minors (those are dropped).
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "spec_minors_integrated": True},
        output={"blocking": False, "has_minor_findings": True},
    )
    assert m.var_mutations["spec_minors_integrated"] is False
    assert isinstance(m.action, Goto)
    assert m.action.target == "plan-writer" and m.action.resume is False


def test_spec_reviewer_clean_no_minors_stops_immediately() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["spec-reviewer"].rules
    # Clean with nothing actionable -> advance to plan-writer, no minors pass.
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "spec_minors_integrated": False},
        output={"blocking": False, "has_minor_findings": False},
    )
    assert isinstance(m.action, Goto)
    assert m.action.target == "plan-writer" and m.action.resume is False
    assert m.var_mutations == {"spec_minors_integrated": False}


def test_front_door_sets_fast_path_from_ceremony() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["front-door"].rules
    express = evaluate_rules(rules, vars=_BASE_VARS,
                             output={"status": "Success", "ceremony": "express"})
    assert express.var_mutations["fast_path"] is True
    full = evaluate_rules(rules, vars=_BASE_VARS,
                          output={"status": "Success", "ceremony": "full"})
    assert full.var_mutations["fast_path"] is False


def test_commit_tool_is_removed_from_registry() -> None:
    # Fix #3: the commit tool is gone; all agents commit with raw git.
    import imp.agents.tools  # noqa: F401 — triggers _register_builtins
    from imp.agents.tools import get_registry
    assert "commit" not in get_registry()


def test_finalize_tails_the_implement_phase() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    impl = next(p for p in wf.phases if p.name == "implement")
    names = [s.name for s in impl.steps]
    assert "stamp-trailers" not in names
    assert names[-1] == "finalize"
    assert names[-2] == "review-base-repin"


def test_no_agent_declares_the_commit_tool() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    for agent in wf.agents.values():
        assert "commit" not in agent.tools, f"{agent.name} still declares commit"


def test_committing_agents_declare_bash() -> None:
    # implementer/task-lead/spec-writer/plan-writer commit with raw git via bash.
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    for name in ("implementer", "task-lead", "spec-writer", "plan-writer"):
        assert "bash" in wf.agents[name].tools, f"{name} missing bash"


def test_plan_verify_commit_fast_path_skips_plan_review() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["plan-verify-commit"].rules
    m = evaluate_rules(rules, vars={**_BASE_VARS, "fast_path": True},
                       output={"exit": 0, "stdout": "", "stderr": ""})
    assert isinstance(m.action, Goto) and m.action.target == "task-lead"


def test_verify_gate_timeout_pauses_not_loops() -> None:
    # run-verify.sh emits 124 (GNU timeout) when the suite outruns the ceiling.
    # Re-running can't beat the clock, so every step that runs it must escalate to a
    # human pause — never bounce the agent back into another doomed run.
    from imp.assets import bundled_workflow_dir
    from imp.engine.types import Pause
    wf = load_workflow(bundled_workflow_dir("imp"))
    steps = _steps(wf)
    vars_ = _BASE_VARS
    for name in ("verify", "sweep-verify", "integration-verify"):
        rules = steps[name].rules
        timed_out = evaluate_rules(rules, vars=vars_,
                                   output={"exit": 124, "stdout": "", "stderr": ""})
        assert isinstance(timed_out.action, Pause), f"{name} must pause on exit 124"
        # A real failure (non-zero, non-124) still routes back to the implementer.
        failed = evaluate_rules(rules, vars=vars_,
                                output={"exit": 1, "stdout": "", "stderr": ""})
        assert not isinstance(failed.action, Pause), (
            f"{name} must NOT pause on an ordinary failure"
        )


def test_plan_reviewer_blocking_reworks_and_clears_flag() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["plan-reviewer"].rules
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "plan_minors_integrated": True},
        output={"blocking": True, "has_minor_findings": False},
    )
    assert isinstance(m.action, Goto)
    assert m.action.target == "plan-writer" and m.action.resume is True
    assert m.var_mutations["plan_minors_integrated"] is False


def test_plan_reviewer_clean_with_minors_integrates_then_owes_review() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["plan-reviewer"].rules
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "plan_minors_integrated": False},
        output={"blocking": False, "has_minor_findings": True},
    )
    assert m.var_mutations["plan_minors_integrated"] is True
    assert isinstance(m.action, Goto)
    assert m.action.target == "plan-writer" and m.action.resume is True


def test_plan_reviewer_verification_pass_stops_and_drops_minors() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["plan-reviewer"].rules
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "plan_minors_integrated": True},
        output={"blocking": False, "has_minor_findings": True},
    )
    assert m.var_mutations["plan_minors_integrated"] is False
    assert isinstance(m.action, Goto)
    assert m.action.target == "task-lead" and m.action.resume is False


def test_plan_reviewer_clean_no_minors_stops_immediately() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["plan-reviewer"].rules
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "plan_minors_integrated": False},
        output={"blocking": False, "has_minor_findings": False},
    )
    assert isinstance(m.action, Goto)
    assert m.action.target == "task-lead" and m.action.resume is False
    assert m.var_mutations == {}


def test_spec_writer_prompt_has_minors_integration_branch() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    body = wf.agents["spec-writer"].prompt_template_source
    # The clean-review branch keys on the injected `blocking:` line and integrates minors.
    assert "blocking: false" in body
    assert "minors" in body.lower()
    assert "without committing" in body.lower()


def test_plan_writer_prompt_has_minors_integration_branch() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    body = wf.agents["plan-writer"].prompt_template_source
    assert "blocking: false" in body
    assert "minors" in body.lower()
    assert "without committing" in body.lower()


def test_spec_writer_prompt_drops_title_and_auto_titled() -> None:
    # Acceptance #1: the spec-writer system prompt no longer surfaces the
    # change title or auto_titled flag anywhere in its body. The agent learns
    # the user's intent from the first user reply, not from the prompt.
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    body = wf.agents["spec-writer"].prompt_template_source
    assert "{{ change.title }}" not in body
    assert "{{ change.auto_titled }}" not in body


def test_spec_writer_prompt_drops_auto_titled_conditional() -> None:
    # Acceptance #2: the {% if change.auto_titled %} / {% else %} / {% endif %}
    # split is gone; slug-settling guidance is a single unconditional section.
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    body = wf.agents["spec-writer"].prompt_template_source
    assert "change.auto_titled" not in body
    assert "{% if change.auto_titled" not in body


def test_spec_writer_prompt_mandates_opening_question() -> None:
    # Acceptance #3: the prompt explicitly instructs the agent to open the
    # change by calling wait_for_user (not via a bare turn-end), with no
    # codebase exploration before the user answers.
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    body = wf.agents["spec-writer"].prompt_template_source
    lowered = body.lower()
    assert "open by asking" in lowered
    assert "first action" in lowered
    # The discipline requires wait_for_user for the opening question.
    assert "wait_for_user" in lowered
    # No codebase exploration before the user has answered.
    assert "do not" in lowered or "do not\n" in lowered or "do not " in lowered
    assert "codebase" in lowered


def test_spec_writer_prompt_mandates_proactive_rename() -> None:
    # Acceptance #4: the agent renames proactively via rename_change as soon
    # as intent is clear, ANNOUNCING the name in prose WITHOUT asking the
    # user. The new guidance (name-on-intent) instructs rename+commit same turn.
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    body = wf.agents["spec-writer"].prompt_template_source
    lowered = body.lower()
    assert "without asking the user" in lowered
    assert "announce" in lowered  # "Announce the name in prose"
    # Name-on-intent: rename_change applies immediately and rename+commit happen
    # this same turn (the old iron-rule forbidding same-turn is now inverted).
    assert "this same turn" in body
    assert "immediately" in body


def test_spec_writer_prompt_drops_skip_reflexive_paragraph() -> None:
    # Acceptance #5 (part 1): the "Skip reflexive questions on fully-specified
    # requests" paragraph is incompatible with always-ask and must be removed.
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    body = wf.agents["spec-writer"].prompt_template_source
    assert "Skip reflexive questions" not in body
    assert "fully-specified requests" not in body


def test_spec_writer_prompt_has_trivial_carveout() -> None:
    # Acceptance #5 (part 2): criterion 1 of the complexity rubric gains a
    # carve-out clarifying that the mandatory opening question does not count
    # against `trivial`.
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    body = wf.agents["spec-writer"].prompt_template_source
    lowered = body.lower()
    # "opening question" appears as both the discipline name and the carve-out.
    assert "opening question" in lowered
    # The carve-out must explicitly say the opening question does not count.
    assert "does not count" in lowered or "does NOT count" in body


def test_spec_writer_prompt_has_no_slug_interpolation() -> None:
    from pathlib import Path
    src = Path("src/imp/assets/workflows/imp/agents/spec-writer.md")
    text = src.read_text()
    assert "{{ change.slug }}" not in text
    assert "{{change.slug}}" not in text
    # And no leftover "provisional slug" framing.
    assert "provisional" not in text.lower()


def test_no_goto_spec_writer_edge() -> None:
    from pathlib import Path
    wf = Path("src/imp/assets/workflows/imp/workflow.yaml").read_text()
    # Rework edges back into spec-writer must be `resume:` (transcript kept), so a
    # resumed spec-writer always has its rename_change result in history. The one
    # permitted `goto: spec-writer` is front-door's forward entry on the full path.
    assert wf.count("goto: spec-writer") == 1  # only front-door's forward entry
    assert "goto: spec_writer" not in wf


def test_plan_writer_declares_wait_for_user() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    assert "wait_for_user" in wf.agents["plan-writer"].tools


def test_review_cadence_vars_declared() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    d = wf.vars.defaults()
    assert d["review_scope"] == "plan"
    assert d["review_base"] == ""
    assert "tasks_since_review" not in d   # removed: review is per-plan, no counter
    assert "review_batch_size" not in d
    assert "panel_done" not in d            # removed: review-panel keys on review_scope only
    assert "multi_plan" not in d            # removed: plan-cycle-check owns coverage


def test_integration_fork_var_declared_default_empty() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    assert wf.vars.defaults()["integration_fork"] == ""


def test_dogfood_workflow_install_is_symlink_to_assets() -> None:
    # Acceptance #1/#2/#6: in this repo, imp/workflows/imp must be a
    # symlink whose resolved target equals src/imp/assets/workflows/imp.
    # This pins the dogfood install against silent drift from the bundled
    # assets — if someone re-materializes the directory, this test fails.
    repo_root = Path(__file__).resolve().parents[1]
    link = repo_root / "imp" / "workflows" / "imp"
    target = repo_root / "src" / "imp" / "assets" / "workflows" / "imp"
    assert link.is_symlink(), f"{link} is not a symlink"
    assert os.readlink(link) == "../../src/imp/assets/workflows/imp", (
        f"unexpected symlink literal target: {os.readlink(link)!r}"
    )
    assert link.resolve() == target.resolve(), (
        f"{link} resolves to {link.resolve()}, expected {target.resolve()}"
    )


def test_spec_verify_commit_non_fast_path_continues_to_spec_reviewer() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["spec-verify-commit"].rules
    # Non-fast path, clean commit -> continue (falls through to spec-reviewer).
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "fast_path": False},
        output={"exit": 0, "stdout": "", "stderr": ""},
    )
    assert isinstance(m.action, Continue)


def test_plan_writer_is_first_plan_phase_step() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    plan_phase = next(p for p in wf.phases if p.name == "plan")
    assert plan_phase.steps[0].name == "plan-writer"


def test_verify_step_has_max_loops_5() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    # A non-converging verify -> task-lead cycle pauses in minutes, overriding the
    # workflow default of 1000.
    assert _steps(wf)["verify"].max_loops == 5


def test_task_lead_declares_verify_tolerated_output() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    assert "verify_tolerated" in wf.agents["task-lead"].output_schema.field_names()


def test_task_lead_tolerated_routes_to_cleanup() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["task-lead"].rules
    # All current failures tolerated: task-lead made NO change and routes straight to
    # cleanup-implement, skipping the verify gate; it carries no var.
    m = evaluate_rules(
        rules, vars=_BASE_VARS,
        output={"status": "Success", "deviation": False,
                "verify_tolerated": True, "summary": "tolerated"},
    )
    assert isinstance(m.action, Goto)
    assert m.action.target == "cleanup-implement" and m.action.resume is False
    assert m.var_mutations == {}


def test_task_lead_normal_return_advances_to_verify() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["task-lead"].rules
    # task-lead implements the whole plan in one entry; a normal (non-deviation,
    # non-tolerated) return advances to the verify gate. There is no per-task
    # self-loop — the implement loop lives inside the single entry.
    m = evaluate_rules(
        rules, vars=_BASE_VARS,
        output={"status": "Success", "deviation": False,
                "verify_tolerated": False, "summary": "plan done"},
    )
    assert isinstance(m.action, Continue)


def test_verify_red_resumes_task_lead() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["verify"].rules
    # A red suite resumes task-lead for triage (the three-rule no-carry form).
    m = evaluate_rules(rules, vars=_BASE_VARS,
                       output={"exit": 1, "stdout": "", "stderr": ""})
    assert isinstance(m.action, Goto)
    assert m.action.target == "task-lead" and m.action.resume is True


def test_change_panel_done_var_declared_default_false() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    assert wf.vars.defaults()["change_panel_done"] is False


def test_plan_cycle_first_coverage_complete_runs_full_panel_and_latches() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["plan-cycle-check"].rules
    # Coverage completes for the first time (panel not yet run): the full whole-change
    # panel fires (review_scope=change) and the once-only guard is latched true.
    m = evaluate_rules(
        rules, vars={**_BASE_VARS, "change_panel_done": False},
        output={"more_plans": False, "rationale": "done"})
    assert isinstance(m.action, Goto) and m.action.target == "code-reviewer"
    assert m.var_mutations["review_scope"] == "change"
    assert m.var_mutations["change_panel_done"] is True


def test_plan_cycle_coverage_complete_after_panel_reviews_per_batch() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["plan-cycle-check"].rules
    # The panel already ran once (guard latched): a corrective plan completing after a
    # rework is reviewed as a per-batch diff (review_scope=plan), NOT a fresh full panel.
    m = evaluate_rules(
        rules, vars={**_BASE_VARS, "change_panel_done": True},
        output={"more_plans": False, "rationale": "corrective"})
    assert isinstance(m.action, Goto) and m.action.target == "code-reviewer"
    assert m.var_mutations["review_scope"] == "plan"
    assert "change_panel_done" not in m.var_mutations


def test_review_base_repin_corrective_batch_routes_to_finalize() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["review-base-repin"].rules
    # A per-batch review settling AFTER the full panel (guard latched) means coverage
    # is complete: route to finalize, NOT back to plan-writer.
    m = evaluate_rules(
        rules, vars={**_BASE_VARS, "review_scope": "plan", "change_panel_done": True},
        output={"exit": 0, "stdout": "abc123", "stderr": ""})
    assert isinstance(m.action, Goto) and m.action.target == "finalize"


def test_review_base_repin_mid_change_batch_returns_to_plan_writer() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["review-base-repin"].rules
    # A normal mid-change per-batch review (panel not yet run) loops back to plan-writer
    # for the next plan -- unchanged behavior, guarded against regression.
    m = evaluate_rules(
        rules, vars={**_BASE_VARS, "review_scope": "plan", "change_panel_done": False},
        output={"exit": 0, "stdout": "abc123", "stderr": ""})
    assert isinstance(m.action, Goto) and m.action.target == "plan-writer"


def test_deferred_sweep_declares_sweep_tolerated_output() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    assert "sweep_tolerated" in wf.agents["deferred-sweep"].output_schema.field_names()


def test_deferred_sweep_tolerated_routes_to_cleanup() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["deferred-sweep"].rules
    # All sweep failures tolerated: deferred-sweep made NO fix and routes straight to
    # cleanup-sweep, skipping the sweep-verify gate; it carries no var.
    m = evaluate_rules(
        rules, vars=_BASE_VARS,
        output={"status": "Success", "sweep_tolerated": True, "summary": "tolerated"},
    )
    assert isinstance(m.action, Goto)
    assert m.action.target == "cleanup-sweep" and m.action.resume is False
    assert m.var_mutations == {}


def test_deferred_sweep_normal_return_continues_without_carry() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["deferred-sweep"].rules
    # A normal sweep entry (no toleration) continues to sweep-verify and sets no carry.
    m = evaluate_rules(
        rules, vars=_BASE_VARS,
        output={"status": "Success", "sweep_tolerated": False, "summary": "drained"},
    )
    assert isinstance(m.action, Continue)
    assert "sweep_tolerated_carry" not in m.var_mutations


def test_sweep_verify_has_max_loops_5() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    assert _steps(wf)["sweep-verify"].max_loops == 5


def test_sweep_verify_red_resumes_deferred_sweep() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["sweep-verify"].rules
    m = evaluate_rules(rules, vars=_BASE_VARS,
                       output={"exit": 1, "stdout": "", "stderr": ""})
    assert isinstance(m.action, Goto)
    assert m.action.target == "deferred-sweep" and m.action.resume is True


def test_spec_writer_prompt_has_build_or_save_section() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    body = wf.agents["spec-writer"].prompt_template_source
    lowered = body.lower()
    assert "create_and_enter_worktree" in body
    assert "head_changed" in body

    assert "build this out now" in lowered
    assert "1. **Build it now**" not in body
    assert "2. **Save as a draft**" not in body

    assert "imp resume <id>" in body
    assert "spec phase pauses" in lowered

    assert "going live or parking the draft" not in lowered
    assert "go live" not in lowered
    assert "go-live" not in lowered


def test_dispatched_subagents_carry_frontmatter_labels() -> None:
    from imp.assets import bundled_workflow_dir
    from imp.engine.loader import load_workflow
    wf = load_workflow(bundled_workflow_dir("imp"))
    assert wf.agents["explorer"].label == "exploring codebase"
    assert wf.agents["researcher"].label == "researching"
    assert wf.agents["implementer"].label == "implementing task"
    assert wf.agents["quality-reviewer"].label == "reviewing changes"
    # plan-writer's label is on the workflow step, not the agent frontmatter
    for name, a in wf.agents.items():
        if name == "plan-writer":
            continue
        assert a.label, f"agent {name!r} has no label"


def test_sweep_verify_timeout_pauses() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["sweep-verify"].rules
    m = evaluate_rules(rules, vars=_BASE_VARS,
                       output={"exit": 124, "stdout": "", "stderr": ""})
    assert isinstance(m.action, Pause)


def test_no_bundled_agent_prompt_references_the_change_handle() -> None:
    """Part 2: every agent prompt names artifacts by the bare id. No prompt may
    still reference the removed `change.handle` Jinja var or a `<handle>` stem."""
    from imp.assets import bundled_workflow_dir

    agents = bundled_workflow_dir("imp") / "agents"
    offenders = []
    for p in sorted(agents.glob("*.md")):
        text = p.read_text()
        if "change.handle" in text or "<handle>" in text:
            offenders.append(p.name)
    assert offenders == []


def test_finalize_merge_gate_is_recommendation_anchored() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    body = wf.agents["finalize"].prompt_template_source
    # Removed verbatim example and token scripting.
    assert "Reply `merge`, or describe the rework" not in body
    assert "Merge this into the base branch, or send it back for rework" not in body
    # New instruction: a recommendation-anchored affirmative asked after the walk.
    assert "recommendation-anchored" in body
    assert "after the per-item walk" in body
    # Guardrails retained.
    assert "never announce it" in body
    assert "underspecified" in body



def test_spec_writer_fast_path_signal_hides_internal_label() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    body = wf.agents["spec-writer"].prompt_template_source
    lowered = body.lower()
    # Standard path: say nothing about the classification.
    assert "say nothing to the user about the classification" in lowered
    # Trivial path: a plain-language heads-up, never naming the internal label.
    assert "brief plain-language heads-up" in lowered
    assert "never name the internal" in lowered


def test_review_base_repin_command_cds_into_project_root() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    cmd = _steps(wf)["review-base-repin"].command
    # Script gates run with cwd = the control-root workflow dir, so the command MUST
    # cd into the worktree before reading HEAD, else it resolves the base-branch tip.
    assert cmd == 'cd "$IMP_PROJECT_ROOT" && git rev-parse HEAD'


def test_spec_reviewer_renders_canonical_severity_rubric() -> None:
    from imp.agents.prompts import build_context, render_prompt
    from imp.assets import bundled_workflow_dir
    from imp.engine.loader import load_workflow
    from imp.engine.state import InMemoryEventLog
    wf_dir = bundled_workflow_dir("imp")
    body = load_workflow(wf_dir).agents["spec-reviewer"].prompt_template_source
    ctx = build_context(change_title="t", event_log=InMemoryEventLog())
    out = render_prompt(body, ctx, workflow_dir=wf_dir)
    assert "behavioral impact, not file type" in out
    for term in ("**Critical**", "**Major**", "**Minor**", "**Nit**"):
        assert term in out


def test_review_steps_read_only_tightening():
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    steps = {s.name: s for s in wf.flat_steps()}
    # Write-free, test-free reviewers are locked to a read-only worktree.
    assert steps["spec-reviewer"].read_only == (".",)
    assert steps["plan-reviewer"].read_only == (".",)
    # Reviewers that run tests in-tree stay loose (cannot be confined).
    assert steps["code-reviewer"].read_only == ()
    assert steps["integration-reviewer"].read_only == ()


def test_quality_reviewer_renders_canonical_severity_rubric() -> None:
    from imp.agents.prompts import build_context, render_prompt
    from imp.assets import bundled_workflow_dir
    from imp.engine.loader import load_workflow
    from imp.engine.state import InMemoryEventLog
    wf_dir = bundled_workflow_dir("imp")
    body = load_workflow(wf_dir).agents["quality-reviewer"].prompt_template_source
    ctx = build_context(change_title="t", event_log=InMemoryEventLog())
    out = render_prompt(body, ctx, workflow_dir=wf_dir)
    assert "behavioral impact, not file type" in out
    for term in ("**Critical**", "**Major**", "**Minor**", "**Nit**"):
        assert term in out

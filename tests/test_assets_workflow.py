from __future__ import annotations

import os
import re
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
    "coverage_settled": False,
    "plan_index": 1, "plan_total": 0, "task_total": 0,
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

    top_leaf = BASE_CAPS | {"rename_change", "create_change",
                            "create_and_enter_worktree"}
    top_dispatcher = top_leaf | {"sub_agent"}
    subagents = {
        "coder", "code-reviewer",
    }
    dispatchers = {"task-lead", "deferred-sweep",
                   "plan-writer", "spec-finalizer", "front-door", "finalize"}

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
    spec_finalizer = wf.agents["spec-finalizer"]
    assert "ask_user" not in spec_finalizer.tools
    assert "complete_step" not in spec_finalizer.tools


def test_bundled_workflow_loads_and_producers_resume() -> None:
    from imp.assets import bundled_workflow_dir
    from imp.engine.types import Goto
    wf_dir = bundled_workflow_dir("imp")
    wf = load_workflow(wf_dir)
    by = {s.name: s for s in wf.flat_steps()}
    # producer back-edges resume their target's conversation
    assert isinstance(by["spec-verify-commit"].rules[-1].action, Goto)
    assert by["spec-verify-commit"].rules[-1].action.resume is True   # -> spec-finalizer
    assert by["plan-verify-commit"].rules[-1].action.resume is True   # -> plan-writer
    assert by["verify"].rules[-1].action.resume is True            # -> task-lead
    assert by["spec-reviewer"].rules[0].action.resume is True         # rework -> spec-finalizer
    # phase jumps clear (plain goto): task_gap -> coder (target task-lead),
    # cross-phase plain goto (resume is False)
    assert by["change-review"].rules[0].action.target == "task-lead"
    assert by["change-review"].rules[0].action.resume is False
    # else: approve -> goto deferred-sweep (forward)
    assert isinstance(by["change-review"].rules[-1].action, Goto)
    assert by["change-review"].rules[-1].action.target == "deferred-sweep"
    assert by["spec-verify-commit"].kind == "script"
    # B3: the merge phase is the engine-native authoritative merge.
    assert wf.phases[-1].name == "integrate"
    assert [s.name for s in wf.phases[-1].steps] == [
        "wait-for-head", "integrate-rebase", "reconciler",
        "integration-verify", "integration-reviewer",
        "summarizer", "finalize",
        "cleanup-merge", "autosquash-resolve-merge",
        "assert-stamped", "merge-to-base"]
    assert by["merge-to-base"].kind == "merge_and_exit_worktree"


def test_review_step_has_no_differ_from():
    from imp.assets import bundled_workflow_dir
    from imp.engine.loader import load_workflow
    wf = load_workflow(bundled_workflow_dir("imp"))
    by = {s.name: s for s in wf.flat_steps()}
    assert by["change-review"].differ_from is None


def test_review_step_declares_high_tier():
    from imp.assets import bundled_workflow_dir
    from imp.engine.loader import load_workflow
    wf = load_workflow(bundled_workflow_dir("imp"))
    by = {s.name: s for s in wf.flat_steps()}
    assert by["change-review"].tier == "high"


def test_gap_check_declares_high_tier() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    by = {s.name: s for s in wf.flat_steps()}
    assert by["gap-check"].tier == "high"


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
    spec_finalizer = wf.agents["spec-finalizer"]
    assert "complexity" in spec_finalizer.output_schema.field_names()
    # complexity drives the thin-spec decision only; fast_path is owned by the
    # front-door phase now, so a renamed value no longer silently breaks routing.
    field = next(f for f in spec_finalizer.output_schema.fields if f.name == "complexity")
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
    # Critical/Major -> rework to spec-finalizer; clear the minors-integrated flag so a
    # real fix re-opens the one minors pass.
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "spec_minors_integrated": True},
        output={"blocking": True, "has_minor_findings": False},
    )
    assert isinstance(m.action, Goto)
    assert m.action.target == "spec-finalizer" and m.action.resume is True
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
    assert m.action.target == "spec-finalizer" and m.action.resume is True


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


def test_front_door_authors_summary_on_express() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    body = wf.agents["front-door"].prompt_template_source
    assert "imp artifact-commit" not in body
    assert "{{ change.home }}/artifacts/summary/{{ change.id }}.md" in body
    low = body.lower()
    # Only on the express path; the full path defers to the end-of-change summarizer.
    assert "express path" in low
    assert "summarizer" in low
    assert "full path do **not** write the summary" in body


def test_commit_tool_is_removed_from_registry() -> None:
    # Fix #3: the commit tool is gone; all agents commit with raw git.
    import imp.agents.tools  # noqa: F401 — triggers _register_builtins
    from imp.agents.tools import get_registry
    assert "commit" not in get_registry()


def test_finalize_tails_the_integrate_phase():
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    integ = next(p for p in wf.phases if p.name == "integrate")
    names = [s.name for s in integ.steps]
    assert "stamp-trailers" not in names
    assert names.index("summarizer") + 1 == names.index("finalize")
    impl = next(p for p in wf.phases if p.name == "implement")
    assert "finalize" not in [s.name for s in impl.steps]
    assert "summarizer" not in [s.name for s in impl.steps]


def test_no_agent_declares_the_commit_tool() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    for agent in wf.agents.values():
        assert "commit" not in agent.tools, f"{agent.name} still declares commit"


def test_committing_agents_declare_bash() -> None:
    # coder/task-lead/spec-finalizer/plan-writer commit with raw git via bash.
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    for name in ("coder", "task-lead", "spec-finalizer", "plan-writer"):
        assert "bash" in wf.agents[name].tools, f"{name} missing bash"


def test_plan_verify_commit_fast_path_skips_plan_review() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["plan-verify-commit"].rules
    m = evaluate_rules(rules, vars={**_BASE_VARS, "fast_path": True},
                       output={"exit": 0, "stdout": "", "stderr": ""})
    assert isinstance(m.action, Goto) and m.action.target == "plan-finalized"


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
        # A real failure (non-zero, non-124) still routes back to the coder.
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
    assert m.action.target == "plan-finalized" and m.action.resume is False


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
    assert m.action.target == "plan-finalized" and m.action.resume is False
    assert m.var_mutations == {}


def test_spec_finalizer_prompt_has_minors_integration_branch() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    body = wf.agents["spec-finalizer"].prompt_template_source
    # The clean-review branch keys on the injected `blocking:` line and integrates minors.
    assert "blocking: false" in body
    assert "minors" in body.lower()
    assert "without changing the file" in body.lower()


def test_plan_writer_prompt_has_minors_integration_branch() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    body = wf.agents["plan-writer"].prompt_template_source
    assert "blocking: false" in body
    assert "minors" in body.lower()
    assert "without changing the file" in body.lower()



def test_no_goto_spec_finalizer_edge() -> None:
    from pathlib import Path
    wf = Path("src/imp/assets/workflows/imp/workflow.yaml").read_text()
    # Rework edges back into spec-finalizer must be `resume:` (transcript kept), so a
    # resumed spec-finalizer always has its rename_change result in history. The one
    # permitted `goto: spec-finalizer` is front-door's forward entry on the full path.
    assert wf.count("goto: spec-finalizer") == 1  # only front-door's forward entry
    assert "goto: spec_finalizer" not in wf


def test_plan_writer_does_not_declare_wait_for_user() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    assert "wait_for_user" not in wf.agents["plan-writer"].tools


def test_review_cadence_vars_declared():
    from imp.assets import bundled_workflow_dir
    d = load_workflow(bundled_workflow_dir("imp")).vars.defaults()
    for gone in ("review_scope", "review_base", "tasks_since_review",
                 "review_batch_size", "panel_done", "multi_plan"):
        assert gone not in d, gone


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
        output={"status": "Success", "verify_tolerated": True, "summary": "tolerated"},
    )
    assert isinstance(m.action, Goto)
    assert m.action.target == "cleanup-implement" and m.action.resume is False
    assert m.var_mutations == {}


def test_task_lead_normal_return_advances_to_verify() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["task-lead"].rules
    # task-lead implements the whole plan in one entry; a normal (non-tolerated)
    # return advances to the verify gate. There is no per-task
    # self-loop — the implement loop lives inside the single entry.
    m = evaluate_rules(
        rules, vars=_BASE_VARS,
        output={"status": "Success", "verify_tolerated": False, "summary": "plan done"},
    )
    assert isinstance(m.action, Continue)


def test_task_lead_forward_only_no_deviation() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    tl = wf.agents["task-lead"]
    # The deviation escalation field is gone from the output schema.
    assert "deviation" not in tl.output_schema.field_names()
    # task-lead flows forward only: no rule resumes (or gotos) plan-writer.
    rules = _steps(wf)["task-lead"].rules
    assert not any(
        isinstance(r.action, Goto) and r.action.target == "plan-writer"
        for r in rules
    )


def test_verify_red_resumes_task_lead() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["verify"].rules
    # A red suite resumes task-lead for triage (the three-rule no-carry form).
    m = evaluate_rules(rules, vars=_BASE_VARS,
                       output={"exit": 1, "stdout": "", "stderr": ""})
    assert isinstance(m.action, Goto)
    assert m.action.target == "task-lead" and m.action.resume is True


def test_coverage_settled_var_declared_and_change_panel_done_absent() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    d = wf.vars.defaults()
    assert d["coverage_settled"] is False
    assert "change_panel_done" not in d
    assert "converge_verify_tolerated" not in d
    body = Path("src/imp/assets/workflows/imp/workflow.yaml").read_text()
    assert "change_panel_done" not in body


def test_gap_check_first_coverage_complete_multi_plan_runs_panel_and_latches() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["gap-check"].rules
    # Multi-plan change, coverage complete: the change-wide panel fires
    # (review_scope=change) and coverage_settled latches true.
    m = evaluate_rules(
        rules, vars={**_BASE_VARS, "plan_total": 2},
        output={"status": "Success", "more_plans": False, "rationale": "done",
                "plan_total": 2})
    assert isinstance(m.action, Goto) and m.action.target == "change-review"
    assert m.var_mutations["coverage_settled"] is True
    assert "review_scope" not in m.var_mutations


def test_check_next_plan_settled_routes_to_change_review():
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    step = _steps(wf)["check-next-plan"]
    assert step.kind == "branch"
    m = evaluate_rules(
        step.rules,
        vars={**_BASE_VARS, "coverage_settled": True, "plan_index": 1, "plan_total": 3},
        output={})
    assert isinstance(m.action, Goto) and m.action.target == "change-review"


def test_check_next_plan_mid_change_advances_to_plan_writer():
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    step = _steps(wf)["check-next-plan"]
    m = evaluate_rules(
        step.rules,
        vars={**_BASE_VARS, "coverage_settled": False, "plan_index": 1, "plan_total": 3},
        output={})
    assert isinstance(m.action, Goto) and m.action.target == "plan-writer"
    assert m.var_mutations["plan_index"] == 2


def test_deferred_sweep_declares_sweep_tolerated_output() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    fields = wf.agents["deferred-sweep"].output_schema.field_names()
    assert "sweep_tolerated" in fields
    assert "converge_verify_tolerated" in fields


def test_deferred_sweep_tolerated_routes_to_cleanup() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["deferred-sweep"].rules
    # All sweep failures tolerated: deferred-sweep made NO fix and routes straight to
    # cleanup-sweep, skipping the sweep-verify gate; it carries no var.
    m = evaluate_rules(rules, vars=_BASE_VARS,
                       output={"status": "Success", "sweep_tolerated": True,
                               "converge_verify_tolerated": False})
    assert isinstance(m.action, Goto)
    assert m.action.target == "cleanup-sweep" and m.action.resume is False
    assert m.var_mutations == {}


def test_deferred_sweep_normal_return_continues_without_carry() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["deferred-sweep"].rules
    # A normal sweep entry (no toleration) continues to sweep-verify and sets no carry.
    m = evaluate_rules(rules, vars=_BASE_VARS,
                       output={"status": "Success", "sweep_tolerated": False,
                               "converge_verify_tolerated": False})
    assert isinstance(m.action, Continue)
    assert "sweep_tolerated_carry" not in m.var_mutations


def test_deferred_sweep_slow_tolerated_routes_to_wait_for_head():
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    step = _steps(wf)["deferred-sweep"]
    m = evaluate_rules(step.rules, vars=_BASE_VARS,
                       output={"status": "Success", "sweep_tolerated": False,
                               "converge_verify_tolerated": True})
    assert isinstance(m.action, Goto)
    assert m.action.target == "wait-for-head"


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



def test_dispatched_subagents_carry_frontmatter_labels() -> None:
    from imp.assets import bundled_workflow_dir
    from imp.engine.loader import load_workflow
    wf = load_workflow(bundled_workflow_dir("imp"))
    assert wf.agents["explorer"].label == "exploring codebase"
    assert wf.agents["researcher"].label == "researching"
    assert wf.agents["coder"].label == "coding"
    assert wf.agents["code-reviewer"].label == "reviewing"
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
    # New instruction: a recommendation-anchored affirmative, with the phrasing and
    # yielding conventions deferred to the shared dialogue partial (no inline scripting,
    # no prescribed one-item-per-turn cadence).
    assert "recommendation-anchored" in body
    assert "phrasing and yielding conventions in the dialogue guidance" in body
    assert "one item per turn" not in body
    # Guardrails retained.
    assert "underspecified" in body
    # §2 pulls in the shared live-gate posture partial.
    assert '{% include "partials/live-gate-dialogue.md" %}' in body
    # The per-item disposition menu framing is gone (outcomes are now mapped from
    # the human's natural-language intent, not picked from a presented menu).
    assert "pick one of three" not in body
    # fix/defer/ignore survive as internal outcomes.
    assert "**fix**" in body and "**defer**" in body and "**ignore**" in body
    # The two finalize-only postures live directly in §2.
    assert "Clarifying questions are first-class" in body
    assert "Engage a concern straight" in body
    assert "within normal noise" in body


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
    assert steps["change-review"].read_only == ()
    assert steps["integration-reviewer"].read_only == ()


def test_code_reviewer_renders_canonical_severity_rubric() -> None:
    from imp.agents.prompts import build_context, render_prompt
    from imp.assets import bundled_workflow_dir
    from imp.engine.loader import load_workflow
    from imp.engine.state import InMemoryEventLog
    wf_dir = bundled_workflow_dir("imp")
    body = load_workflow(wf_dir).agents["code-reviewer"].prompt_template_source
    ctx = build_context(change_title="t", event_log=InMemoryEventLog())
    out = render_prompt(body, ctx, workflow_dir=wf_dir)
    assert "behavioral impact, not file type" in out
    for term in ("**Critical**", "**Major**", "**Minor**", "**Nit**"):
        assert term in out


def test_spec_finalizer_replaces_spec_writer() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    assert "spec-finalizer" in wf.agents
    assert "spec-writer" not in wf.agents
    # The finalizer includes the shared design lens but NOT the dialogue partial.
    body = wf.agents["spec-finalizer"].prompt_template_source
    assert '{% include "partials/spec-design-principles.md" %}' in body
    assert "brainstorm-dialogue.md" not in body
    # Brainstorm-entry sections are gone.
    assert "## Brainstorm Discipline" not in body
    assert "## Build now or save for later" not in body
    assert "## Naming the Change" not in body
    # Its finalize core remains.
    assert "Front-door handoff" in body
    assert "imp artifact-commit" not in body
    # Its self-tag in the deferred reader form is renamed.
    assert "DEFERRED spec-finalizer:" in body
    assert "DEFERRED spec-writer:" not in body


def test_workflow_yaml_has_no_spec_writer_identifier() -> None:
    from pathlib import Path
    wf = Path("src/imp/assets/workflows/imp/workflow.yaml").read_text()
    assert "spec-writer" not in wf
    assert "spec_writer" not in wf
    assert wf.count("goto: spec-finalizer") == 1   # only front-door's forward entry


def test_summarizer_immediately_precedes_finalize():
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    integ = next(p for p in wf.phases if p.name == "integrate")
    names = [s.name for s in integ.steps]
    assert names[names.index("summarizer") + 1] == "finalize"


def test_summarizer_declares_medium_tier() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    assert _steps(wf)["summarizer"].tier == "medium"


def test_summarizer_routes_to_finalize_on_success_and_error() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["summarizer"].rules
    for status in ("Success", "Error"):
        m = evaluate_rules(rules, vars=_BASE_VARS, output={"status": status})
        assert isinstance(m.action, Goto)
        assert m.action.target == "finalize"


def test_summarizer_output_schema_is_status_only() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    assert set(wf.agents["summarizer"].output_schema.field_names()) == {"status"}


def test_finalize_drops_built_field() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    assert "built" not in wf.agents["finalize"].output_schema.field_names()


def test_finalize_leads_with_summary_artifact() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    body = wf.agents["finalize"].prompt_template_source
    assert "artifacts/summary/" in body
    low = body.lower()
    # The gate never blocks on a missing summary — it falls back to the audit fields.
    assert "absent" in low
    assert "built" not in low  # no residual references to the removed field


def test_no_wait_for_user_in_workflow_prompts() -> None:
    # AC2: the deleted tool must not survive in any prompt or partial. A grep of
    # the workflow assets for `wait_for_user` returns nothing.
    root = Path("src/imp/assets/workflows")
    offenders = sorted(
        str(p) for p in root.rglob("*.md") if "wait_for_user" in p.read_text()
    )
    assert offenders == []


def test_explorer_nudges_symbol_outline_in_both_copies() -> None:
    for wf in ("imp", "imp-flat"):
        text = Path(f"src/imp/assets/workflows/{wf}/agents/explorer.md").read_text()
        assert "symbol_outline" in text
        assert "before reading full bodies" in text


def test_explorer_opts_into_symbol_outline(tmp_path: Path) -> None:
    asset = Path("src/imp/assets/workflows/imp").resolve()
    shutil.copytree(asset, tmp_path / "wf")
    wf = load_workflow(tmp_path / "wf")
    assert "symbol_outline" in wf.agents["explorer"].tools


def test_task_lead_step_declares_position_label() -> None:
    from imp.assets import bundled_workflow_dir
    from imp.engine.loader import load_workflow
    wf = load_workflow(bundled_workflow_dir("imp"))
    steps = {s.name: s for ph in wf.phases for s in ph.steps}
    assert steps["task-lead"].position_label is True


def test_spec_finalizer_authors_plan_outline() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    body = wf.agents["spec-finalizer"].prompt_template_source
    low = body.lower()
    # The finalized spec carries a coarse, WHAT-only decomposition that seeds plan_total.
    assert "## Plan outline" in body
    assert "what" in low and "not the how" in low
    assert "5\u20138" not in body             # the ~5-8 floor grain is gone
    assert "natural dependency boundary" in low   # sized at natural boundaries now
    assert "no task-count floor" in low
    assert "source of truth" in low
    assert "plan count" in low or "plan_total" in low
    # The handoff turn now routes through outline authoring before committing.
    assert "Author the plan outline" in body
    # spec-finalizer never emits plan_total — the outline (echoed by plan-writer) seeds it.
    assert "plan_total" not in wf.agents["spec-finalizer"].output_schema.field_names()


def test_plan_writer_consumes_outline_with_express_carveout() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    text = wf.agents["plan-writer"].prompt_template_source
    low = text.lower()
    # Full path: read the outline, find this plan's slice, write only the HOW.
    assert "## Plan outline" in text
    assert "slice" in low
    assert "only the **HOW**" in text or "only the how" in low
    # plan_total is the outline count, not an estimate, on the full path.
    assert "outline-derived" in low or "from the outline" in low
    # Express carve-out: no spec-finalizer / no outline -> self-scope, default to 1.
    assert "express" in low
    assert "hard-depend" in low
    assert "self-scope" in low
    # The supercycle sentence is refreshed to the outline-driven loop + gap-check.
    assert "outline-driven plan loop" in low
    assert "gap-check" in low


def test_plan_cycle_check_gone_gap_check_present_and_reachable() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    by = _steps(wf)
    # The retired guard is gone from steps and disk; gap-check replaces it.
    assert "plan-cycle-check" not in by
    agents_dir = bundled_workflow_dir("imp") / "agents"
    assert not (agents_dir / "plan-cycle-check.md").exists()
    assert (agents_dir / "gap-check.md").exists()
    assert "gap-check" in by and by["gap-check"].tier == "high"
    # gap-check is reachable: check-next-plan routes to it on the last outline plan.
    step = _steps(wf)["check-next-plan"]
    m = evaluate_rules(
        step.rules,
        vars={**_BASE_VARS, "coverage_settled": False, "plan_index": 1, "plan_total": 1},
        output={})
    assert isinstance(m.action, Goto) and m.action.target == "gap-check"


def test_gap_check_routing_and_latch() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["gap-check"].rules
    # Gap found -> author a corrective via plan-writer; latch stays false.
    gap = evaluate_rules(rules, vars={**_BASE_VARS, "plan_index": 2, "plan_total": 2},
                         output={"status": "Success", "more_plans": True,
                                 "plan_total": 3, "rationale": "x"})
    assert isinstance(gap.action, Goto) and gap.action.target == "plan-writer"
    assert gap.var_mutations["plan_index"] == 3
    assert "coverage_settled" not in gap.var_mutations
    assert "review_scope" not in gap.var_mutations
    # Clean + multi-plan -> latch coverage_settled + change-wide change-review.
    multi = evaluate_rules(rules, vars={**_BASE_VARS, "plan_total": 2},
                           output={"status": "Success", "more_plans": False,
                                   "plan_total": 2, "rationale": "done"})
    assert isinstance(multi.action, Goto) and multi.action.target == "change-review"
    assert multi.var_mutations["coverage_settled"] is True
    assert "review_scope" not in multi.var_mutations
    # Clean + single-plan -> change-wide review fires too (plan_total gate dropped).
    single = evaluate_rules(rules, vars={**_BASE_VARS, "plan_total": 1},
                            output={"status": "Success", "more_plans": False,
                                    "plan_total": 1, "rationale": "done"})
    assert isinstance(single.action, Goto) and single.action.target == "change-review"
    assert single.var_mutations["coverage_settled"] is True
    assert "review_scope" not in single.var_mutations


def test_gap_check_agent_is_end_of_change_backstop() -> None:
    text = (Path("src/imp/assets/workflows/imp/agents") / "gap-check.md").read_text()
    low = text.lower()
    assert "name: gap-check" in text
    assert "end-of-change" in low or "end of change" in low
    assert "once" in low
    assert "outline" in low
    assert "plan-cycle-check" not in low          # no residual old name
    assert "task-lead" not in low                 # the dropped agent is not referenced
    assert "plan-code-reviewer" in text           # the code-reviewer rename target
    assert "sole" in low and "completeness" in low
    assert "uncovered" in low
    assert "needs-decision" in low and "suppress" in low
    assert "more_plans" in low
    # Sizing projection is at natural boundaries now — no ~5-8 floor grain.
    assert "5\u20138" not in text
    assert "natural dependency boundaries" in low
    assert "no task-count floor" in low
    # Clamp rules survive verbatim.
    assert "the current plan number" in low
    assert "strictly greater" in low
    assert "coverage complete" in low


def test_task_lead_escalates_coder_tier_for_single_task_plan(tmp_path: Path) -> None:
    from imp.agents.prompts import build_context, render_prompt
    from imp.assets import bundled_workflow_dir
    from imp.engine.state import JsonlEventLog

    body = load_workflow(bundled_workflow_dir("imp")).agents["task-lead"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")

    single = render_prompt(body, build_context(
        change_title="t", event_log=log, change_id="abc",
        change_home=".imp/changes/abc",
        vars={"task_total": 1, "fast_path": False}),
        workflow_dir=bundled_workflow_dir("imp"))
    assert 'subagent_name="coder", tier="high"' in single

    multi = render_prompt(body, build_context(
        change_title="t", event_log=log, change_id="abc",
        change_home=".imp/changes/abc",
        vars={"task_total": 3, "fast_path": False}),
        workflow_dir=bundled_workflow_dir("imp"))
    assert 'subagent_name="coder", tier="medium"' in multi
    assert 'subagent_name="coder", tier="high"' not in multi


def test_imp_gate_steps_pass_tier_arg() -> None:
    from imp.assets import bundled_workflow_dir
    steps = _steps(load_workflow(bundled_workflow_dir("imp")))
    assert steps["verify"].command == "./scripts/run-verify.sh fast"
    assert steps["sweep-verify"].command == "./scripts/run-verify.sh fast"
    assert steps["converge-verify"].command == "./scripts/run-verify.sh slow"
    assert steps["integration-verify"].command == "./scripts/run-verify.sh slow"


def test_imp_flat_gate_steps_pass_tier_arg() -> None:
    from imp.assets import bundled_workflow_dir
    steps = _steps(load_workflow(bundled_workflow_dir("imp-flat")))
    assert steps["verify"].command == "./scripts/run-verify.sh fast"
    assert steps["integration-verify"].command == "./scripts/run-verify.sh slow"
    assert "sweep-verify" not in steps   # imp-flat has no sweep-verify gate


def test_no_legacy_verify_surface_under_src_imp() -> None:
    # The verify_command/verify_timeout/IMP_VERIFY_* surface is fully removed;
    # guard production code AND bundled workflow assets against its return.
    root = Path(__file__).resolve().parents[1] / "src" / "imp"
    pattern = re.compile(r"verify_command|verify_timeout|IMP_VERIFY_")
    offenders = [
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.suffix in {".py", ".md", ".yaml", ".yml", ".sh", ".toml"}
        and pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []

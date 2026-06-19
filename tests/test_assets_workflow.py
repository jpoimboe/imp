from __future__ import annotations

import os
import shutil
from pathlib import Path

from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.types import Continue, Goto


def _steps(wf):
    return {s.name: s for s in wf.flat_steps()}


# All declared workflow vars at their defaults — a realistic evaluate_rules fixture.
_BASE_VARS = {
    "fast_path": False, "spec_minors_integrated": False,
    "plan_minors_integrated": False, "plan_panel_done": False,
    "review_scope": "plan",
    "review_base": "", "tasks_since_review": 0, "review_batch_size": 7,
    "verify_tolerated_carry": False,
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

    top_leaf = BASE_CAPS | {"wait_for_user", "rename_change", "create_change"}
    top_dispatcher = top_leaf | {"task"}
    subagents = {
        "implementer", "quality-reviewer", "plan-reviewer",
    }
    dispatchers = {"task-lead", "deferred-sweep",
                   "plan-writer", "spec-writer", "plan-review-panel"}

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
    assert by["review"].rules[0].action.target == "task-lead"     # task_gap -> implementer
    assert by["review"].rules[0].action.resume is False           # cross-phase plain goto
    # else: approve -> continue (deferred-sweep); plan_gap route is removed
    assert isinstance(by["review"].rules[-1].action, Continue)
    assert by["spec-verify-commit"].kind == "script"
    # B3: the merge phase is the engine-native authoritative merge.
    assert wf.phases[-1].name == "merge"
    assert [s.name for s in wf.phases[-1].steps] == [
        "wait-for-head", "integrate-rebase", "integrator", "integration-review",
        "integration-verify", "bypass-dequeue", "integration-resolve", "merge-to-base"]
    assert by["merge-to-base"].kind == "merge"


def test_review_step_has_no_differ_from():
    from imp.assets import bundled_workflow_dir
    from imp.engine.loader import load_workflow
    wf = load_workflow(bundled_workflow_dir("imp"))
    by = {s.name: s for s in wf.flat_steps()}
    assert by["review"].differ_from is None


def test_review_step_declares_high_tier():
    from imp.assets import bundled_workflow_dir
    from imp.engine.loader import load_workflow
    wf = load_workflow(bundled_workflow_dir("imp"))
    by = {s.name: s for s in wf.flat_steps()}
    assert by["review"].tier == "high"


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
    # Pin the enum shape too: the downstream fast_path set: gates on the exact
    # values, so a renamed/added value would silently break that wiring.
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


def test_plan_panel_done_var_declared_default_false() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    assert wf.vars.defaults()["plan_panel_done"] is False


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
    assert m.action.target == "baseline-verify" and m.action.resume is False


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
    assert m.action.target == "baseline-verify" and m.action.resume is False
    assert m.var_mutations == {"spec_minors_integrated": False}


def test_spec_writer_sets_fast_path_on_trivial_written() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["spec-writer"].rules
    # The spec-writer rule sets fast_path from output.complexity only.
    m = evaluate_rules(rules, vars=_BASE_VARS,
                       output={"phase": "written", "complexity": "trivial",
                               "status": "Success"})
    assert m.var_mutations["fast_path"] is True

    m2 = evaluate_rules(rules, vars=_BASE_VARS,
                        output={"phase": "written", "complexity": "standard",
                                "status": "Success"})
    assert m2.var_mutations["fast_path"] is False


def test_commit_tool_is_removed_from_registry() -> None:
    # Fix #3: the commit tool is gone; all agents commit with raw git.
    import imp.agents.tools  # noqa: F401 — triggers _register_builtins
    from imp.agents.tools import get_registry
    assert "commit" not in get_registry()


def test_stamp_trailers_heads_the_review_phase() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    review = next(p for p in wf.phases if p.name == "review")
    assert review.steps[0].name == "stamp-trailers"
    impl = next(p for p in wf.phases if p.name == "implement")
    assert "stamp-trailers" not in [s.name for s in impl.steps]


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

    # Timeout pauses regardless of carry state (rule ordering: exit:124 precedes
    # the carry rule, so a timeout with carry=True still pauses).
    with_carry = evaluate_rules(steps["verify"].rules,
                                vars={**_BASE_VARS, "verify_tolerated_carry": True},
                                output={"exit": 124, "stdout": "", "stderr": ""})
    assert isinstance(with_carry.action, Pause)


def test_plan_reviewer_blocking_reworks_and_clears_flag() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["plan-review"].rules
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "plan_minors_integrated": True},
        output={"blocking": True, "has_minor_findings": False},
    )
    assert isinstance(m.action, Goto)
    assert m.action.target == "plan-writer" and m.action.resume is True
    assert m.var_mutations["plan_minors_integrated"] is False
    assert m.var_mutations["plan_panel_done"] is True


def test_plan_reviewer_clean_with_minors_integrates_then_owes_review() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["plan-review"].rules
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "plan_minors_integrated": False},
        output={"blocking": False, "has_minor_findings": True},
    )
    assert m.var_mutations["plan_minors_integrated"] is True
    assert m.var_mutations["plan_panel_done"] is True
    assert isinstance(m.action, Goto)
    assert m.action.target == "plan-writer" and m.action.resume is True


def test_plan_reviewer_verification_pass_stops_and_drops_minors() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["plan-review"].rules
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "plan_minors_integrated": True},
        output={"blocking": False, "has_minor_findings": True},
    )
    assert m.var_mutations["plan_minors_integrated"] is False
    assert m.var_mutations["plan_panel_done"] is False
    assert isinstance(m.action, Goto)
    assert m.action.target == "task-lead" and m.action.resume is False


def test_plan_reviewer_clean_no_minors_stops_immediately() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["plan-review"].rules
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "plan_minors_integrated": False},
        output={"blocking": False, "has_minor_findings": False},
    )
    assert isinstance(m.action, Goto)
    assert m.action.target == "task-lead" and m.action.resume is False
    assert m.var_mutations == {"plan_panel_done": False}


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
    assert "{% else %}" not in body


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
    # Every edge back into spec-writer must be a `resume:` (transcript kept), so a
    # resumed spec-writer always has its rename_change result in history and never
    # needs the slug from the system prompt. A fresh `goto: spec-writer` would
    # drop the transcript and reintroduce the dependency Part A removes.
    assert "goto: spec-writer" not in wf
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
    assert d["tasks_since_review"] == 0
    assert d["review_batch_size"] == 7
    assert "panel_done" not in d            # removed: review-panel keys on review_scope only
    assert "multi_plan" not in d        # removed: plan-cycle-check owns coverage


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


def test_spec_verify_commit_fast_path_skips_to_baseline_verify() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["spec-verify-commit"].rules
    # Clean commit on the fast path -> baseline gate, skipping spec-reviewer.
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "fast_path": True},
        output={"exit": 0, "stdout": "", "stderr": ""},
    )
    assert isinstance(m.action, Goto)
    assert m.action.target == "baseline-verify" and m.action.resume is False


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


def test_autosquash_non_fast_path_goes_to_review_base_init() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["autosquash"].rules
    m = evaluate_rules(rules, vars={**_BASE_VARS, "fast_path": False},
                       output={"exit": 0, "stdout": "", "stderr": ""})
    assert isinstance(m.action, Goto)
    assert m.action.target == "review-base-init" and m.action.resume is False


def test_autosquash_fast_path_goes_to_review_base_init() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["autosquash"].rules
    m = evaluate_rules(rules, vars={**_BASE_VARS, "fast_path": True},
                       output={"exit": 0, "stdout": "", "stderr": ""})
    assert isinstance(m.action, Goto)
    assert m.action.target == "review-base-init" and m.action.resume is False


def test_spec_writer_writes_under_artifacts(tmp_path: Path) -> None:
    asset = Path("src/imp/assets/workflows/imp").resolve()
    shutil.copytree(asset, tmp_path / "wf")
    wf = load_workflow(tmp_path / "wf")
    perms = _steps(wf)["spec-writer"].fs_permissions
    write_paths = [p for perm in perms for p in perm.paths
                   if "write" in perm.operations]
    assert any(".imp/change/artifacts/spec/" in p for p in write_paths)
    assert all("imp/specs/" not in p for p in write_paths)


def test_plan_writer_writes_under_artifacts(tmp_path: Path) -> None:
    asset = Path("src/imp/assets/workflows/imp").resolve()
    shutil.copytree(asset, tmp_path / "wf")
    wf = load_workflow(tmp_path / "wf")
    perms = _steps(wf)["plan-writer"].fs_permissions
    write_paths = [p for perm in perms for p in perm.paths
                   if "write" in perm.operations]
    assert any(".imp/change/artifacts/plans/" in p for p in write_paths)
    assert all("imp/plans/" not in p for p in write_paths)


def test_baseline_verify_is_first_plan_phase_step() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    plan_phase = next(p for p in wf.phases if p.name == "plan")
    assert plan_phase.steps[0].name == "baseline-verify"
    assert plan_phase.steps[1].name == "plan-writer"
    assert _steps(wf)["baseline-verify"].command == "./scripts/baseline-verify.sh"


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


def test_verify_tolerated_carry_var_declared_default_false() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    assert wf.vars.defaults()["verify_tolerated_carry"] is False


def test_task_lead_tolerated_routes_back_through_verify() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["task-lead"].rules
    # All current failures tolerated: task-lead made NO change and routes BACK to
    # verify carrying the signal (NOT forward to autosquash). The engine resets a
    # step's loop counter only when THAT step forward-advances, so routing back lets
    # the *verify* step perform the budget-resetting forward advance on its next pass.
    m = evaluate_rules(
        rules, vars=_BASE_VARS,
        output={"status": "Success", "done": True, "deviation": False,
                "verify_tolerated": True, "summary": "tolerated"},
    )
    assert isinstance(m.action, Goto)
    assert m.action.target == "verify" and m.action.resume is False
    assert m.var_mutations["verify_tolerated_carry"] is True


def test_task_lead_normal_return_does_not_route_to_verify() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["task-lead"].rules
    # A normal "more tasks remain" return loops back to task-lead, untouched by the
    # verify_tolerated rule, and sets no carry.
    m = evaluate_rules(
        rules, vars=_BASE_VARS,
        output={"status": "Success", "done": False, "deviation": False,
                "verify_tolerated": False, "summary": "next"},
    )
    assert isinstance(m.action, Goto) and m.action.target == "task-lead"
    assert "verify_tolerated_carry" not in m.var_mutations


def test_verify_carry_continues_forward_and_clears_carry() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["verify"].rules
    # A red suite (exit 1) whose failures task-lead already tolerated (carry set):
    # forward-continue past verify -- the advance that resets verify's OWN loop
    # counter -- and clear the carry so the next batch's first verify starts fresh.
    m = evaluate_rules(rules, vars={**_BASE_VARS, "verify_tolerated_carry": True},
                       output={"exit": 1, "stdout": "", "stderr": ""})
    assert isinstance(m.action, Continue)
    assert m.var_mutations["verify_tolerated_carry"] is False


def test_verify_red_without_carry_resumes_task_lead() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["verify"].rules
    # A red suite with no tolerated-carry resumes task-lead for triage -- the
    # backward edge that increments verify's loop counter.
    m = evaluate_rules(rules, vars={**_BASE_VARS, "verify_tolerated_carry": False},
                       output={"exit": 1, "stdout": "", "stderr": ""})
    assert isinstance(m.action, Goto)
    assert m.action.target == "task-lead" and m.action.resume is True


def test_verify_green_clears_stale_carry() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["verify"].rules
    # A green suite (exit 0) clears a stale carry too, so a carry set in a prior
    # batch can never leak forward and mask a real regression.
    m = evaluate_rules(rules, vars={**_BASE_VARS, "verify_tolerated_carry": True},
                       output={"exit": 0, "stdout": "", "stderr": ""})
    assert isinstance(m.action, Continue)
    assert m.var_mutations["verify_tolerated_carry"] is False


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
        output={"more_plans": False, "running_tasks": 3, "rationale": "done"})
    assert isinstance(m.action, Goto) and m.action.target == "review"
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
        output={"more_plans": False, "running_tasks": 1, "rationale": "corrective"})
    assert isinstance(m.action, Goto) and m.action.target == "review"
    assert m.var_mutations["review_scope"] == "plan"
    assert "change_panel_done" not in m.var_mutations


def test_review_base_repin_corrective_batch_routes_to_finalize() -> None:
    from imp.assets import bundled_workflow_dir
    wf = load_workflow(bundled_workflow_dir("imp"))
    rules = _steps(wf)["review-base-repin"].rules
    # A per-batch review settling AFTER the full panel (guard latched) means coverage
    # is complete: route to stamp-trailers -> finalize, NOT back to plan-writer.
    m = evaluate_rules(
        rules, vars={**_BASE_VARS, "review_scope": "plan", "change_panel_done": True},
        output={"exit": 0, "stdout": "abc123", "stderr": ""})
    assert isinstance(m.action, Goto) and m.action.target == "stamp-trailers"


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


def test_spec_writer_can_write_progress(tmp_path: Path) -> None:
    asset = Path("src/imp/assets/workflows/imp").resolve()
    shutil.copytree(asset, tmp_path / "wf")
    wf = load_workflow(tmp_path / "wf")
    perms = _steps(wf)["spec-writer"].fs_permissions
    write_paths = [p for perm in perms for p in perm.paths
                   if "write" in perm.operations]
    assert ".imp/change/progress.md" in write_paths
    # still scoped to its own artifact dir, not the whole tree
    assert any(".imp/change/artifacts/spec/" in p for p in write_paths)
    assert "**" not in write_paths


def test_plan_writer_can_write_progress(tmp_path: Path) -> None:
    asset = Path("src/imp/assets/workflows/imp").resolve()
    shutil.copytree(asset, tmp_path / "wf")
    wf = load_workflow(tmp_path / "wf")
    perms = _steps(wf)["plan-writer"].fs_permissions
    write_paths = [p for perm in perms for p in perm.paths
                   if "write" in perm.operations]
    assert ".imp/change/progress.md" in write_paths
    assert any(".imp/change/artifacts/plans/" in p for p in write_paths)
    assert "**" not in write_paths


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
    "fast_path": False, "ready_to_merge": False, "final_review": True,
    "spec_final_review_done": False, "plan_final_review_done": False,
}


def test_shipped_default_workflow_loads(tmp_path: Path) -> None:
    asset = Path("src/imp/assets/workflows/default").resolve()
    shutil.copytree(asset, tmp_path / "wf")
    wf = load_workflow(tmp_path / "wf")
    assert wf.name
    assert wf.phases
    for agent in wf.agents.values():
        assert "complete_step" in agent.tools, f"{agent.name} missing complete_step"


def test_spec_writer_does_not_declare_ask_user(tmp_path: Path) -> None:
    asset = Path("src/imp/assets/workflows/default").resolve()
    shutil.copytree(asset, tmp_path / "wf")
    wf = load_workflow(tmp_path / "wf")
    spec_writer = wf.agents["spec-writer"]
    assert "ask_user" not in spec_writer.tools
    assert "complete_step" in spec_writer.tools


def test_bundled_workflow_loads_and_producers_resume() -> None:
    from imp.assets import default_workflow_dir
    from imp.engine.types import Goto
    wf_dir = default_workflow_dir()
    wf = load_workflow(wf_dir)
    by = {s.name: s for s in wf.flat_steps()}
    # producer back-edges resume their target's conversation
    assert isinstance(by["spec-verify-commit"].rules[-1].action, Goto)
    assert by["spec-verify-commit"].rules[-1].action.resume is True   # -> spec-writer
    assert by["plan-verify-commit"].rules[-1].action.resume is True   # -> plan-writer
    assert by["run-tests"].rules[-1].action.resume is True            # -> task-lead
    assert by["spec-reviewer"].rules[0].action.resume is True         # rework -> spec-writer
    # phase jumps clear (plain goto)
    assert by["code-reviewer"].rules[1].action.resume is False    # rework -> plan-writer
    assert by["spec-verify-commit"].kind == "script"
    assert wf.phases[-1].name == "merge" and wf.phases[-1].steps == ()


def test_default_workflow_has_code_review_constraint():
    from imp.assets import default_workflow_dir
    from imp.engine.loader import load_workflow
    wf = load_workflow(default_workflow_dir())
    # code-reviewer must differ from task-lead (the implementer step tier) for
    # cross-provider review; the reviewer is constrained to differ from task-lead.
    by = {s.name: s for s in wf.flat_steps()}
    assert by["code-reviewer"].differ_from == "task-lead"


def test_default_code_reviewer_step_declares_tier():
    # In the feature workflow tier is assigned at the step level in
    # workflow.yaml, not in the agent frontmatter.  Verify the step-level
    # declaration is present and correct so code_review diversity wiring works.
    from imp.assets import default_workflow_dir
    from imp.engine.loader import load_workflow
    wf = load_workflow(default_workflow_dir())
    by = {s.name: s for s in wf.flat_steps()}
    assert by["code-reviewer"].tier == "high"


def test_default_workflow_loads() -> None:
    # Verify the default workflow loads without errors.
    from imp.assets import default_workflow_dir
    from imp.engine.loader import load_workflow
    wf = load_workflow(default_workflow_dir())
    assert wf.name == "feature"
    assert len(wf.flat_steps()) > 0


def test_spec_writer_declares_complexity_output() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    spec_writer = wf.agents["spec-writer"]
    assert "complexity" in spec_writer.output_schema.field_names()
    # Pin the enum shape too: the downstream fast_path set: gates on the exact
    # values, so a renamed/added value would silently break that wiring.
    field = next(f for f in spec_writer.output_schema.fields if f.name == "complexity")
    assert field.type == "enum"
    assert field.values == ("trivial", "standard")
    assert field.required is True


def test_fast_path_var_declared_default_false() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    assert wf.vars.defaults()["fast_path"] is False


def test_final_review_done_vars_declared_default_false() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    defaults = wf.vars.defaults()
    assert defaults["spec_final_review_done"] is False
    assert defaults["plan_final_review_done"] is False


def test_spec_gates_precede_reviewer_in_order() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    spec = next(p for p in wf.phases if p.name == "spec")
    names = [s.name for s in spec.steps]
    # final-review gate first, then fast-path gate, then the reviewer
    assert names.index("spec-final-review-gate") < names.index("fast-path-spec-gate")
    assert names.index("fast-path-spec-gate") < names.index("spec-reviewer")


def test_fast_path_spec_gate_skips_to_plan_writer() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    gate = _steps(wf)["fast-path-spec-gate"].rules[0]
    assert isinstance(gate.action, Goto) and gate.action.target == "plan-writer"


def test_spec_final_review_gate_consumes_and_resets_flag() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    rules = _steps(wf)["spec-final-review-gate"].rules
    # flag set -> reset to false and goto plan-writer (skipping the reviewer)
    m = evaluate_rules(rules, vars={**_BASE_VARS, "spec_final_review_done": True}, output=None)
    assert m.var_mutations["spec_final_review_done"] is False
    assert isinstance(m.action, Goto)
    assert m.action.target == "plan-writer"
    assert m.action.resume is False
    # flag clear -> continue, no mutation
    m2 = evaluate_rules(rules, vars=_BASE_VARS, output=None)
    assert isinstance(m2.action, Continue)
    assert m2.var_mutations == {}


def test_spec_reviewer_clean_sets_flag_and_resumes_writer() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    rules = _steps(wf)["spec-reviewer"].rules
    # rework branch unchanged: blocking true -> resume spec-writer, no mutation
    rework = evaluate_rules(rules, vars=_BASE_VARS, output={"blocking": True})
    assert isinstance(rework.action, Goto) and rework.action.resume is True
    assert rework.var_mutations == {}
    # clean branch (the change): blocking false -> set flag + resume spec-writer
    clean = evaluate_rules(rules, vars=_BASE_VARS, output={"blocking": False})
    assert clean.var_mutations["spec_final_review_done"] is True
    assert isinstance(clean.action, Goto)
    assert clean.action.target == "spec-writer"
    assert clean.action.resume is True


def test_spec_writer_sets_fast_path_on_trivial_written() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    rules = _steps(wf)["spec-writer"].rules
    m = evaluate_rules(rules, vars=_BASE_VARS,
                       output={"phase": "written", "complexity": "trivial", "status": "Success"})
    assert m.var_mutations["fast_path"] is True

    m2 = evaluate_rules(rules, vars=_BASE_VARS,
                        output={"phase": "written", "complexity": "standard", "status": "Success"})
    assert m2.var_mutations["fast_path"] is False


def test_commit_tool_is_removed_from_registry() -> None:
    # Fix #3: the commit tool is gone; all agents commit with raw git.
    import imp.agents.tools  # noqa: F401 — triggers _register_builtins
    from imp.agents.tools import get_registry
    assert "commit" not in get_registry()


def test_review_phase_stamps_trailers_after_autosquash() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    review = next(p for p in wf.phases if p.name == "review")
    names = [s.name for s in review.steps]
    assert "stamp-trailers" in names, names
    assert names.index("stamp-trailers") == names.index("autosquash") + 1, names
    assert names.index("stamp-trailers") < names.index("code-reviewer"), names


def test_no_agent_declares_the_commit_tool() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    for agent in wf.agents.values():
        assert "commit" not in agent.tools, f"{agent.name} still declares commit"


def test_committing_agents_declare_bash() -> None:
    # implementer/task-lead/spec-writer/plan-writer commit with raw git via bash.
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    for name in ("implementer", "task-lead", "spec-writer", "plan-writer"):
        assert "bash" in wf.agents[name].tools, f"{name} missing bash"


def test_fast_path_gates_skip_depth_keep_safety() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    steps = _steps(wf)
    plan_gate = steps["fast-path-plan-gate"].rules[0]
    assert isinstance(plan_gate.action, Goto) and plan_gate.action.target == "task-lead"
    cycle_gate = steps["fast-path-cycle-gate"].rules[0]
    assert isinstance(cycle_gate.action, Goto) and cycle_gate.action.target == "review"
    assert "code-reviewer" in steps
    assert wf.phases[-1].name == "merge"


def test_plan_gates_precede_reviewer_in_order() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    plan = next(p for p in wf.phases if p.name == "plan")
    names = [s.name for s in plan.steps]
    # final-review gate first, then fast-path gate, then the reviewer
    assert names.index("plan-final-review-gate") < names.index("fast-path-plan-gate")
    assert names.index("fast-path-plan-gate") < names.index("plan-review")


def test_plan_final_review_gate_consumes_and_resets_flag() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    rules = _steps(wf)["plan-final-review-gate"].rules
    # flag set -> reset to false and goto task-lead (skipping the reviewer)
    m = evaluate_rules(rules, vars={**_BASE_VARS, "plan_final_review_done": True}, output=None)
    assert m.var_mutations["plan_final_review_done"] is False
    assert isinstance(m.action, Goto)
    assert m.action.target == "task-lead"
    assert m.action.resume is False
    # flag clear -> continue, no mutation
    m2 = evaluate_rules(rules, vars=_BASE_VARS, output=None)
    assert isinstance(m2.action, Continue)
    assert m2.var_mutations == {}


def test_plan_reviewer_clean_sets_flag_and_resumes_writer() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    rules = _steps(wf)["plan-review"].rules
    # rework branch unchanged: blocking true -> resume plan-writer, no mutation
    rework = evaluate_rules(rules, vars=_BASE_VARS, output={"blocking": True})
    assert isinstance(rework.action, Goto) and rework.action.resume is True
    assert rework.var_mutations == {}
    # clean branch (the change): blocking false -> set flag + resume plan-writer
    clean = evaluate_rules(rules, vars=_BASE_VARS, output={"blocking": False})
    assert clean.var_mutations["plan_final_review_done"] is True
    assert isinstance(clean.action, Goto)
    assert clean.action.target == "plan-writer"
    assert clean.action.resume is True


def test_spec_writer_prompt_has_polish_pass_branch() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    body = wf.agents["spec-writer"].prompt_template_source
    # The polish branch keys on the injected `blocking:` line and pins phase/complexity.
    assert "blocking: false" in body
    assert "polish" in body.lower()
    assert "without committing" in body.lower()


def test_plan_writer_prompt_has_polish_pass_branch() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    body = wf.agents["plan-writer"].prompt_template_source
    assert "blocking: false" in body
    assert "polish" in body.lower()
    assert "without committing" in body.lower()


def test_spec_writer_prompt_drops_title_and_auto_titled() -> None:
    # Acceptance #1: the spec-writer system prompt no longer surfaces the
    # change title or auto_titled flag anywhere in its body. The agent learns
    # the user's intent from the first user reply, not from the prompt.
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    body = wf.agents["spec-writer"].prompt_template_source
    assert "{{ change.title }}" not in body
    assert "{{ change.auto_titled }}" not in body


def test_spec_writer_prompt_drops_auto_titled_conditional() -> None:
    # Acceptance #2: the {% if change.auto_titled %} / {% else %} / {% endif %}
    # split is gone; slug-settling guidance is a single unconditional section.
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    body = wf.agents["spec-writer"].prompt_template_source
    assert "change.auto_titled" not in body
    assert "{% if change.auto_titled" not in body
    assert "{% else %}" not in body


def test_spec_writer_prompt_mandates_opening_question() -> None:
    # Acceptance #3: the prompt explicitly instructs the agent to open the
    # change with a single plain-prose question, with no tool calls and no
    # codebase exploration before the user answers.
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    body = wf.agents["spec-writer"].prompt_template_source
    lowered = body.lower()
    assert "open by asking" in lowered
    assert "first turn" in lowered
    # The discipline forbids any tool use or codebase exploration on turn 1.
    assert "no tool calls" in lowered
    assert "no codebase exploration" in lowered


def test_spec_writer_prompt_mandates_proactive_rename() -> None:
    # Acceptance #4: the agent renames proactively via rename_change as soon
    # as intent is clear, ANNOUNCING the name in prose WITHOUT asking the
    # user. The new guidance (name-on-intent) instructs rename+commit same turn.
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
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
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    body = wf.agents["spec-writer"].prompt_template_source
    assert "Skip reflexive questions" not in body
    assert "fully-specified requests" not in body


def test_spec_writer_prompt_has_trivial_carveout() -> None:
    # Acceptance #5 (part 2): criterion 1 of the complexity rubric gains a
    # carve-out clarifying that the mandatory opening question does not count
    # against `trivial`.
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    body = wf.agents["spec-writer"].prompt_template_source
    lowered = body.lower()
    # "opening question" appears as both the discipline name and the carve-out.
    assert "opening question" in lowered
    # The carve-out must explicitly say the opening question does not count.
    assert "does not count" in lowered or "does NOT count" in body


def test_spec_writer_prompt_has_no_slug_interpolation() -> None:
    from pathlib import Path
    src = Path("src/imp/assets/workflows/default/agents/spec-writer.md")
    text = src.read_text()
    assert "{{ change.slug }}" not in text
    assert "{{change.slug}}" not in text
    # And no leftover "provisional slug" framing.
    assert "provisional" not in text.lower()


def test_no_goto_spec_writer_edge() -> None:
    from pathlib import Path
    wf = Path("src/imp/assets/workflows/default/workflow.yaml").read_text()
    # Every edge back into spec-writer must be a `resume:` (transcript kept), so a
    # resumed spec-writer always has its rename_change result in history and never
    # needs the slug from the system prompt. A fresh `goto: spec-writer` would
    # drop the transcript and reintroduce the dependency Part A removes.
    assert "goto: spec-writer" not in wf
    assert "goto: spec_writer" not in wf


def test_dogfood_workflow_install_is_symlink_to_assets() -> None:
    # Acceptance #1/#2/#6: in this repo, imp/workflows/default must be a
    # symlink whose resolved target equals src/imp/assets/workflows/default.
    # This pins the dogfood install against silent drift from the bundled
    # assets — if someone re-materializes the directory, this test fails.
    repo_root = Path(__file__).resolve().parents[1]
    link = repo_root / "imp" / "workflows" / "default"
    target = repo_root / "src" / "imp" / "assets" / "workflows" / "default"
    assert link.is_symlink(), f"{link} is not a symlink"
    assert os.readlink(link) == "../../src/imp/assets/workflows/default", (
        f"unexpected symlink literal target: {os.readlink(link)!r}"
    )
    assert link.resolve() == target.resolve(), (
        f"{link} resolves to {link.resolve()}, expected {target.resolve()}"
    )

from __future__ import annotations

import shutil
from pathlib import Path

from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.types import Goto


def _steps(wf):
    return {s.name: s for s in wf.flat_steps()}


# All declared workflow vars at their defaults — a realistic evaluate_rules fixture.
_BASE_VARS = {"fast_path": False, "ready_to_merge": False, "final_review": True}


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
    # code_review must differ from fast (the implementer subagent tier) for
    # cross-provider review; in the feature workflow the coder runs at the
    # `fast` tier, so the reviewer is constrained against `fast` not `code`.
    assert wf.model_rules["code_review"].differ_provider_from == "fast"


def test_default_code_reviewer_step_declares_model_role():
    # In the feature workflow model_role is assigned at the step level in
    # workflow.yaml, not in the agent frontmatter.  Verify the step-level
    # declaration is present and correct so code_review diversity wiring works.
    from imp.assets import default_workflow_dir
    from imp.engine.loader import load_workflow
    wf = load_workflow(default_workflow_dir())
    by = {s.name: s for s in wf.flat_steps()}
    assert by["code-reviewer"].model_role == "code_review"


def test_default_workflow_declares_tier_menu() -> None:
    from imp.assets import default_workflow_dir
    from imp.engine.loader import load_workflow
    wf = load_workflow(default_workflow_dir())
    assert wf.model_tiers == ("fast", "balanced", "powerful")


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


def test_spec_writer_drafting_turn_never_evaluates_set() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    rules = _steps(wf)["spec-writer"].rules
    m = evaluate_rules(rules, vars=_BASE_VARS,
                       output={"phase": "drafting", "complexity": "trivial", "status": "Success"})
    assert m.var_mutations == {}


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

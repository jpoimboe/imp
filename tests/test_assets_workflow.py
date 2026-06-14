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
    "plan_minors_integrated": False, "panel_done": False,
    "multi_plan": False, "spec_confirm": True, "spec_unseen_changes": False,
    "review_scope": "plan",
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
    assert by["verify"].rules[-1].action.resume is True            # -> task-lead
    assert by["spec-reviewer"].rules[0].action.resume is True         # rework -> spec-writer
    # phase jumps clear (plain goto)
    assert by["review"].rules[1].action.resume is False   # plan_gap -> plan-writer (plain goto)
    assert by["spec-verify-commit"].kind == "script"
    # B3: the merge phase is the engine-native authoritative merge.
    assert wf.phases[-1].name == "merge"
    assert [s.name for s in wf.phases[-1].steps] == ["base-fresh-check", "merge-to-base"]
    assert by["merge-to-base"].kind == "merge"


def test_review_step_has_no_differ_from():
    from imp.assets import default_workflow_dir
    from imp.engine.loader import load_workflow
    wf = load_workflow(default_workflow_dir())
    by = {s.name: s for s in wf.flat_steps()}
    assert by["review"].differ_from is None


def test_review_step_declares_medium_tier():
    from imp.assets import default_workflow_dir
    from imp.engine.loader import load_workflow
    wf = load_workflow(default_workflow_dir())
    by = {s.name: s for s in wf.flat_steps()}
    assert by["review"].tier == "medium"


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


def test_minors_integrated_vars_declared_default_false() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    defaults = wf.vars.defaults()
    assert defaults["spec_minors_integrated"] is False
    assert defaults["plan_minors_integrated"] is False



def test_spec_reviewer_blocking_reworks_and_clears_flag() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
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
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
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
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
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
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    rules = _steps(wf)["spec-reviewer"].rules
    # Clean with nothing actionable -> advance to plan-writer, no minors pass.
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "spec_minors_integrated": False},
        output={"blocking": False, "has_minor_findings": False},
    )
    assert isinstance(m.action, Goto)
    assert m.action.target == "plan-writer" and m.action.resume is False
    assert m.var_mutations == {"spec_minors_integrated": False, "spec_unseen_changes": False}


def test_spec_writer_sets_fast_path_on_trivial_written() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    rules = _steps(wf)["spec-writer"].rules
    # _BASE_VARS already has spec_unseen_changes/gate flags False and spec_confirm
    # True, which is exactly the trivial-written precondition — no overrides needed.
    m = evaluate_rules(rules, vars=_BASE_VARS,
                       output={"phase": "written", "complexity": "trivial",
                               "status": "Success", "unseen_changes": False})
    assert m.var_mutations["fast_path"] is True

    m2 = evaluate_rules(rules, vars=_BASE_VARS,
                        output={"phase": "written", "complexity": "standard",
                                "status": "Success", "unseen_changes": False})
    assert m2.var_mutations["fast_path"] is False


def test_commit_tool_is_removed_from_registry() -> None:
    # Fix #3: the commit tool is gone; all agents commit with raw git.
    import imp.agents.tools  # noqa: F401 — triggers _register_builtins
    from imp.agents.tools import get_registry
    assert "commit" not in get_registry()


def test_implement_phase_stamps_trailers_after_sweep_verify() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    impl = next(p for p in wf.phases if p.name == "implement")
    names = [s.name for s in impl.steps]
    assert names.index("sweep-verify") < names.index("stamp-trailers"), names
    assert names.index("stamp-trailers") < names.index("plan-cycle-check"), names


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


def test_plan_verify_commit_fast_path_skips_plan_review() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    rules = _steps(wf)["plan-verify-commit"].rules
    m = evaluate_rules(rules, vars={**_BASE_VARS, "fast_path": True},
                       output={"exit": 0, "stdout": "", "stderr": ""})
    assert isinstance(m.action, Goto) and m.action.target == "task-lead"


def test_plan_reviewer_blocking_reworks_and_clears_flag() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    rules = _steps(wf)["plan-review"].rules
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "plan_minors_integrated": True},
        output={"blocking": True, "has_minor_findings": False},
    )
    assert isinstance(m.action, Goto)
    assert m.action.target == "plan-writer" and m.action.resume is True
    assert m.var_mutations["plan_minors_integrated"] is False


def test_plan_reviewer_clean_with_minors_integrates_then_owes_review() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    rules = _steps(wf)["plan-review"].rules
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "plan_minors_integrated": False},
        output={"blocking": False, "has_minor_findings": True},
    )
    assert m.var_mutations["plan_minors_integrated"] is True
    assert isinstance(m.action, Goto)
    assert m.action.target == "plan-writer" and m.action.resume is True


def test_plan_reviewer_verification_pass_stops_and_drops_minors() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    rules = _steps(wf)["plan-review"].rules
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "plan_minors_integrated": True},
        output={"blocking": False, "has_minor_findings": True},
    )
    assert m.var_mutations["plan_minors_integrated"] is False
    assert isinstance(m.action, Goto)
    assert m.action.target == "task-lead" and m.action.resume is False


def test_plan_reviewer_clean_no_minors_stops_immediately() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    rules = _steps(wf)["plan-review"].rules
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "plan_minors_integrated": False},
        output={"blocking": False, "has_minor_findings": False},
    )
    assert isinstance(m.action, Goto)
    assert m.action.target == "task-lead" and m.action.resume is False
    assert m.var_mutations == {}


def test_spec_writer_prompt_has_minors_integration_branch() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    body = wf.agents["spec-writer"].prompt_template_source
    # The clean-review branch keys on the injected `blocking:` line and integrates minors.
    assert "blocking: false" in body
    assert "minors" in body.lower()
    assert "without committing" in body.lower()


def test_plan_writer_prompt_has_minors_integration_branch() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    body = wf.agents["plan-writer"].prompt_template_source
    assert "blocking: false" in body
    assert "minors" in body.lower()
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
    # change by calling wait_for_user (not via a bare turn-end), with no
    # codebase exploration before the user answers.
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
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


def test_plan_writer_declares_wait_for_user() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    assert "wait_for_user" in wf.agents["plan-writer"].tools


def test_review_scope_declared_default_plan() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    assert wf.vars.defaults()["multi_plan"] is False
    assert wf.vars.defaults()["review_scope"] == "plan"


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


def test_spec_verify_commit_fast_path_no_unseen_skips_to_plan_writer() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    rules = _steps(wf)["spec-verify-commit"].rules
    # Clean commit, fast path, no unseen spec change -> straight to planning,
    # skipping spec-reviewer entirely.
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "fast_path": True, "spec_unseen_changes": False},
        output={"exit": 0, "stdout": "", "stderr": ""},
    )
    assert isinstance(m.action, Goto)
    assert m.action.target == "plan-writer" and m.action.resume is False


def test_spec_verify_commit_fast_path_unseen_change_forwards_confirm_gate() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    rules = _steps(wf)["spec-verify-commit"].rules
    # Fast path, confirm enabled, an undirected spec change pending -> still routes
    # through spec-confirm (preserving spec-reviewer's gate), clearing the flag.
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "fast_path": True, "spec_confirm": True,
              "spec_unseen_changes": True},
        output={"exit": 0, "stdout": "", "stderr": ""},
    )
    assert isinstance(m.action, Goto)
    assert m.action.target == "spec-confirm" and m.action.resume is False
    assert m.var_mutations["spec_unseen_changes"] is False


def test_spec_verify_commit_fast_path_confirm_disabled_skips_to_plan_writer() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    rules = _steps(wf)["spec-verify-commit"].rules
    # Fast path with confirm DISABLED: even an unseen change goes straight to
    # planning -- the user opted out of the confirm gate.
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "fast_path": True, "spec_confirm": False,
              "spec_unseen_changes": True},
        output={"exit": 0, "stdout": "", "stderr": ""},
    )
    assert isinstance(m.action, Goto)
    assert m.action.target == "plan-writer" and m.action.resume is False


def test_spec_verify_commit_non_fast_path_continues_to_spec_reviewer() -> None:
    from imp.assets import default_workflow_dir
    wf = load_workflow(default_workflow_dir())
    rules = _steps(wf)["spec-verify-commit"].rules
    # Non-fast path, clean commit -> continue (falls through to spec-reviewer).
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "fast_path": False},
        output={"exit": 0, "stdout": "", "stderr": ""},
    )
    assert isinstance(m.action, Continue)

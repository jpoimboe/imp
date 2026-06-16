"""Structural gate for the shipped feature workflow (spec §4, §11): it loads
and validates against the roles `imp init` scaffolds, with all phases, the
supercycle back-edges, and the discipline skills present. No live providers."""

from __future__ import annotations

from imp.agents.skills import list_skill_names
from imp.assets import bundled_workflow_dir
from imp.engine.loader import load_workflow

# The tiers the workflow.yaml references; all are scaffolded by cli/init.py.
_TIERS_SET = frozenset({"low", "medium", "high"})


def _wf():
    return load_workflow(bundled_workflow_dir("imp"), configured_tiers=_TIERS_SET)


def test_workflow_loads_with_scaffolded_roles() -> None:
    wf = _wf()
    assert wf.name == "feature"
    assert [p.name for p in wf.phases] == ["spec", "plan", "implement",
                                           "review", "merge"]


def test_referenced_roles_are_a_subset_of_init_scaffold() -> None:
    # Every tier used must be one the provider-code floor supplies (else change
    # creation would LoadError). The floor's tier taxonomy is low/medium/high.
    scaffolded = set(_TIERS_SET)
    used = {s.tier for p in _wf().phases for s in p.steps
            if s.kind == "agent" and s.tier}
    assert used <= scaffolded, f"roles not scaffolded by init: {used - scaffolded}"


def test_supercycle_back_edges_present() -> None:
    # Spot-check the load-bearing loop edges survive parsing.
    steps = {s.name: s for p in _wf().phases for s in p.steps}
    # plan-cycle-check loops to plan-writer; the merged review + deferred-sweep run per plan.
    assert "plan-cycle-check" in steps and "review" in steps and "deferred-sweep" in steps
    assert "task-lead" in steps and "finalize" in steps


def test_skills_shipped() -> None:
    names = set(list_skill_names(bundled_workflow_dir("imp") / "skills"))
    assert {"test-driven-development", "systematic-debugging",
            "verification-before-completion", "receiving-code-review"} <= names


def test_every_declared_tool_is_real() -> None:
    # The loader accepts ANY string in `tools:` (loader.py:105) — tool-name
    # validation is deferred to the provider's _build_tools at RUNTIME
    # (anthropic.py:137-138, KeyError on unknown). So a typo'd or renamed tool
    # would survive the whole non-live suite and only fail in the live dogfood.
    # This gate checks tool names itself to close that gap.
    from imp.agents.tools import get_registry
    registry = set(get_registry())
    # Auto-injected (complete_step/task/load_skill/wait_for_user) + provider-native
    # reserved names (web_search/web_fetch, web_native.py) are valid but not in
    # the @imp_tool registry (they are synthesized per-runner at runtime).
    reserved = {"complete_step", "task", "load_skill", "wait_for_user",
                "web_search", "web_fetch"}
    allowed = registry | reserved
    bad = {a.name: sorted(set(a.tools) - allowed)
           for a in _wf().agents.values() if set(a.tools) - allowed}
    assert not bad, f"agents declare unknown tools: {bad}"


def test_no_orphan_agent_files() -> None:
    # Every agents/*.md is referenced by a step or declared as a subagent —
    # catches a deleted-but-referenced or created-but-unwired agent.
    # Coupling note: this compares file STEMS to step file.stem U subagent NAMES;
    # the port keeps every agent's frontmatter `name:` == its filename stem, so the
    # two coincide. A future file whose name diverges from its stem would trip this.
    wf = _wf()
    referenced = {s.file.stem for p in wf.phases for s in p.steps
                  if s.kind == "agent" and s.file}
    referenced |= {name for a in wf.agents.values() for name in a.subagents}
    on_disk = {p.stem for p in (bundled_workflow_dir("imp") / "agents").glob("*.md")}
    assert on_disk == referenced, (
        f"orphan agent files: {on_disk - referenced}; "
        f"missing files: {referenced - on_disk}"
    )


def test_finalize_is_high_tier_and_routes_merge_rework_pause() -> None:
    from imp.engine.gates import evaluate_rules
    from imp.engine.types import Goto, Pause
    wf = _wf()
    steps = {s.name: s for s in wf.flat_steps()}
    step = steps["finalize"]
    assert step.kind == "agent" and step.tier == "high"
    assert step.file is not None and step.file.stem == "finalize"
    agent = wf.agents["finalize"]
    decision = next(f for f in agent.output_schema.fields if f.name == "decision")
    assert decision.type == "enum"
    assert set(decision.values) == {"merge", "rework", "pause"}
    base = {v.name: wf.vars.defaults()[v.name] for v in wf.vars.decls}
    out = {"status": "Success", "decision": "merge", "comments": "",
           "built": "", "verify_status": "", "open_items": "", "next_steps": ""}
    m = evaluate_rules(step.rules, vars=base, output=out)
    assert isinstance(m.action, Goto) and m.action.target == "merge"
    m2 = evaluate_rules(step.rules, vars=base, output={**out, "decision": "rework"})
    assert isinstance(m2.action, Goto) and m2.action.target == "plan-writer"
    m3 = evaluate_rules(step.rules, vars=base, output={**out, "decision": "pause"})
    assert isinstance(m3.action, Pause)


def test_deleted_merge_vars_absent_and_no_artifacts_paths() -> None:
    wf = load_workflow(bundled_workflow_dir("imp"))
    names = {v.name for v in wf.vars.decls}
    for gone in ("ready_to_merge", "final_review", "integration_needs_review"):
        assert gone not in names, gone


def test_no_deleted_var_names_anywhere_in_workflow_assets() -> None:
    wf_dir = bundled_workflow_dir("imp")
    banned = ["ready_to_merge", "final_review", "integration_pass",
              "integration_needs_review", "spec_gate_pending", "spec_gate_passed"]
    targets = [wf_dir / "workflow.yaml"]
    targets += sorted((wf_dir / "agents").glob("*.md"))
    targets += sorted((wf_dir / "scripts").glob("*.sh"))
    for path in targets:
        body = path.read_text()
        for name in banned:
            assert name not in body, f"{name} still appears in {path.name}"


def test_no_agent_prompt_uses_artifacts_namespace() -> None:
    for md in (bundled_workflow_dir("imp") / "agents").glob("*.md"):
        body = md.read_text()
        assert "{{ artifacts" not in body, md.name

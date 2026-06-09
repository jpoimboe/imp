"""Structural gate for the shipped feature workflow (spec §4, §11): it loads
and validates against the roles `imp init` scaffolds, with all phases, the
supercycle back-edges, and the discipline skills present. No live providers."""

from __future__ import annotations

from imp.agents.skills import list_skill_names
from imp.assets import default_workflow_dir
from imp.engine.loader import load_workflow

# The tiers the workflow.yaml references; all are scaffolded by cli/init.py.
_TIERS_SET = frozenset({"low", "medium", "high"})


def _wf():
    return load_workflow(default_workflow_dir(), configured_tiers=_TIERS_SET)


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
    # plan-cycle-check loops to plan-writer; review-panel/recheck route task_gap/plan_gap.
    assert "plan-cycle-check" in steps and "review-panel" in steps and "recheck" in steps
    assert "task-lead" in steps and "final-review" in steps


def test_skills_shipped() -> None:
    names = set(list_skill_names(default_workflow_dir() / "skills"))
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
    on_disk = {p.stem for p in (default_workflow_dir() / "agents").glob("*.md")}
    assert on_disk == referenced, (
        f"orphan agent files: {on_disk - referenced}; "
        f"missing files: {referenced - on_disk}"
    )


def test_default_workflow_has_merge_var_and_no_artifacts_paths() -> None:
    wf = load_workflow(default_workflow_dir())
    assert any(v.name == "ready_to_merge" for v in wf.vars.decls)
    text = (default_workflow_dir() / "workflow.yaml").read_text()
    assert "/artifacts/" not in text


def test_no_agent_prompt_uses_artifacts_namespace() -> None:
    for md in (default_workflow_dir() / "agents").glob("*.md"):
        body = md.read_text()
        assert "{{ artifacts" not in body, md.name
        assert "/artifacts/" not in body, md.name

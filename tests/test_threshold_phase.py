from __future__ import annotations

from pathlib import Path

from imp.agents.prompts import build_context, render_prompt
from imp.assets import bundled_workflow_dir
from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.state import JsonlEventLog
from imp.engine.types import Goto

FRONT_DOOR = Path("src/imp/assets/partials/front-door.md")

_BASE_VARS = {
    "fast_path": False, "review_scope": "plan",
    "review_base": "",
    "coverage_settled": False, "integration_fork": "",
}


def _wf():
    return load_workflow(bundled_workflow_dir("default"))


def _steps(wf):
    return {s.name: s for s in wf.flat_steps()}


def test_threshold_is_first_phase_with_show_label_false() -> None:
    wf = _wf()
    assert wf.phases[0].name == "threshold"
    step = wf.phases[0].steps[0]
    assert step.name == "threshold"
    assert step.kind == "agent"
    assert step.show_label is False           # inherits the phase's show_label:false default
    assert step.label is None
    assert step.phase_label is None


def test_threshold_express_routes_to_scope_gate_and_sets_fast_path() -> None:
    rules = _steps(_wf())["threshold"].rules
    m = evaluate_rules(rules, vars=_BASE_VARS,
                       output={"status": "Success", "ceremony": "express"})
    assert isinstance(m.action, Goto)
    assert m.action.target == "scope-code-check"  # gate first; then spec-written, plan-writer
    assert m.action.resume is False
    assert m.var_mutations["fast_path"] is True


def test_threshold_full_routes_to_scope_gate_and_clears_fast_path() -> None:
    rules = _steps(_wf())["threshold"].rules
    m = evaluate_rules(rules, vars=_BASE_VARS,
                       output={"status": "Success", "ceremony": "full"})
    assert isinstance(m.action, Goto)
    assert m.action.target == "scope-code-check"  # gate first; then spec-finalizer on a clean tree
    assert m.action.resume is False
    assert m.var_mutations["fast_path"] is False


def test_threshold_declares_ceremony_output() -> None:
    agent = _wf().agents["threshold"]
    field = next(f for f in agent.output_schema.fields if f.name == "ceremony")
    assert field.type == "enum"
    assert field.values == ("express", "full")
    assert field.required is True


def test_spec_finalizer_no_longer_sets_fast_path() -> None:
    # Ownership of fast_path moved to threshold; spec-finalizer's terminal rule
    # only continues to the verify gate now.
    rules = _steps(_wf())["spec-finalizer"].rules
    m = evaluate_rules(rules, vars=_BASE_VARS,
                       output={"phase": "written", "complexity": "trivial",
                               "status": "Success"})
    assert "fast_path" not in m.var_mutations


def test_threshold_agent_prose_carries_the_three_hats_and_handoff() -> None:
    fd = FRONT_DOOR.read_text()
    body = _wf().agents["threshold"].prompt_template_source
    low = fd.lower()
    for hat in ("concierge", "scout", "brainstorm"):
        assert hat in low, hat
    assert "rename_quest" in fd                        # names the change
    assert "{{ quest.home }}/artifacts/spec/{{ quest.id }}.md" in fd
    assert "never surface these internal terms" in low
    assert "announce the name in prose without asking permission" in fd
    assert '{% include "partials/go-live-worktree.md" %}' in body  # go-live step stays in the tail


def test_threshold_adoption_section_omitted_on_autonomous_runs(tmp_path) -> None:
    body = _wf().agents["threshold"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")

    attended = render_prompt(body, build_context(
        quest_title="t", event_log=log, quest_id="abc",
        quest_home=".imp/quests/abc"),
        workflow_dir=bundled_workflow_dir("default"))
    # Attended runs render the adoption-first section.
    assert "## Keep discovered work here by default" in attended
    assert "imp new --backlog" in attended

    autonomous = render_prompt(body, build_context(
        quest_title="t", event_log=log, quest_id="abc",
        quest_home=".imp/quests/abc", autonomous=True),
        workflow_dir=bundled_workflow_dir("default"))
    # Autonomous runs omit the whole {% if not run.autonomous %} block.
    assert "## Keep discovered work here by default" not in autonomous
    assert "imp new --backlog" not in autonomous
    # The retired unconditional / spin-off-first heading and phrasing are gone.
    assert "## Splitting off a separate change" not in attended
    assert "ending your turn to yield** before calling `create_quest`" not in attended


def test_threshold_has_no_headless_branch(tmp_path) -> None:
    body = _wf().agents["threshold"].prompt_template_source
    assert "run.non_interactive" not in body
    assert "## Headless runs" not in body
    log = JsonlEventLog(tmp_path / "events.jsonl")
    for autonomous in (False, True):
        out = render_prompt(body, build_context(
            quest_title="t", event_log=log, quest_id="abc",
            quest_home=".imp/quests/abc", autonomous=autonomous),
            workflow_dir=bundled_workflow_dir("default")).lower()
        assert "the ceremony is predetermined" not in out
        assert 'ceremony="full"' not in out


def test_threshold_includes_both_brainstorm_partials() -> None:
    fd = FRONT_DOOR.read_text()
    assert '{% include "partials/spec-design-principles.md" %}' in fd
    assert '{% include "partials/brainstorm-dialogue.md" %}' in fd


def test_threshold_includes_shared_front_door_partial(tmp_path) -> None:
    body = _wf().agents["threshold"].prompt_template_source
    assert '{% include "partials/front-door.md" %}' in body
    log = JsonlEventLog(tmp_path / "events.jsonl")
    out = render_prompt(body, build_context(
        quest_title="t", event_log=log, quest_id="abc",
        quest_home=".imp/quests/abc"),
        workflow_dir=bundled_workflow_dir("default"))
    # front-door body renders inline — behaviorally identical to before.
    assert "## Orienting before you offer" in out
    assert "## Acting on imp" in out
    assert "## Speak plainly" in out


def test_front_door_carries_the_workflow_agnostic_body() -> None:
    fd = FRONT_DOOR.read_text()
    for inc in ("coding-stance", "action-care", "imp-grounding",
                "spec-design-principles", "brainstorm-dialogue",
                "memory-authoring", "sandbox-limits"):
        assert f'{{% include "partials/{inc}.md" %}}' in fd, inc
    for heading in ("## Orienting before you offer", "## Acting on imp",
                    "## Speak plainly", "## Naming the quest early",
                    "## When the work depends on another quest"):
        assert heading in fd, heading
    # workflow-specific tail sections must NOT leak into the shared body.
    assert "## Classifying the draft's shape" not in fd
    assert "## Going live (the build path)" not in fd
    assert "create_and_enter_worktree" not in fd
    assert "## Output" not in fd


def test_threshold_tail_keeps_workflow_specific_sections() -> None:
    body = _wf().agents["threshold"].prompt_template_source
    assert "## Classifying the draft's shape" in body
    assert "## Going live (the build path)" in body
    assert '{% include "partials/go-live-worktree.md" %}' in body
    assert "## Output" in body
    # the extracted body no longer lives inline in threshold.md.
    assert "## Orienting before you offer" not in body
    assert "## Acting on imp" not in body


def test_threshold_renders_full_brainstorm_depth(tmp_path) -> None:
    body = _wf().agents["threshold"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    out = render_prompt(body, build_context(
        quest_title="t", event_log=log, quest_id="abc",
        quest_home=".imp/quests/abc"),
        workflow_dir=bundled_workflow_dir("default"))
    low = out.lower()
    # dialogue conduct expanded from brainstorm-dialogue
    assert "one question at a time" in low
    assert "**Grill the design.**" in out
    assert "present it in sections" not in out
    assert "build now" in low
    assert "final readback before committing" in low
    # design lens expanded from spec-design-principles
    assert "architecture, components, data flow, error handling, and testing" in out
    assert "do not propose unrelated refactoring" not in out
    assert "improvements to the code this quest touches." in out


def test_threshold_owns_complexity_classification() -> None:
    body = _wf().agents["threshold"].prompt_template_source
    low = body.lower()
    # threshold decides the draft's shape (thin vs four-part) and never leaks the label.
    assert "trivial" in low and "standard" in low
    assert "goal + acceptance" in low or "goal and acceptance" in low
    assert "never name the internal classification" in low


def test_threshold_classifies_by_blast_radius_not_by_conversation() -> None:
    body = _wf().agents["threshold"].prompt_template_source
    # the prose is hard-wrapped; compare against a whitespace-flattened copy.
    flat = " ".join(body.split())
    # the two retired tests: whether a conversation happened, and a bare "positive reason"
    assert "no clarifying questions" not in body
    assert "you have a positive reason" not in flat
    assert (
        "A **trivial** quest — small, localized, cheaply reversible, with every value "
        "it changes already pinned in the conversation — gets a thin draft: "
        "**Goal + Acceptance criteria** only."
    ) in flat
    assert (
        "**Judge by blast radius, not by how much you talked.** Asking questions and "
        "enumerating values makes a change *safer*, not bigger: a quest whose whole "
        "diff is values you listed and the user confirmed stays trivial however long "
        "the conversation ran. What lifts a quest out of trivial is **reach** — "
        "behavior other code depends on, a persisted format, a public or agent-facing "
        "interface, control flow, or anything that would break far from the lines you "
        "touched. Fixed strings and comments nothing branches on sit at "
        "the bottom of that scale; engine, schema, and workflow-routing changes never "
        "reach it."
    ) in flat
    assert (
        "Raise it only when the quest clears the trivial bar above on **blast radius** "
        "— small, localized, reversible, every value pinned, nothing downstream "
        "depending on what changes — and then only in plain language, "
        "recommendation-anchored, never a scripted menu or the internal labels."
    ) in flat
    # the handed-spec carve-out and the standard-shape sentences ride through unchanged
    assert (
        "A spec the user handed you (named for you to read, or pasted or pointed to in "
        "the conversation) is never such a reason: it hasn't been through imp's own "
        "review, which is exactly what the lighter path skips — stay on the full pass "
        "and don't raise it."
    ) in flat
    assert (
        "Anything else is **standard** and gets the full four-part draft (Goal, Scope, "
        "Decisions summary, Acceptance criteria). A large-but-precisely-specified "
        "quest is `standard`, not trivial — low ambiguity is not low complexity."
    ) in flat


def test_threshold_prose_names_spec_finalizer_not_spec_writer(tmp_path) -> None:
    body = _wf().agents["threshold"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    out = render_prompt(body, build_context(
        quest_title="t", event_log=log, quest_id="abc",
        quest_home=".imp/quests/abc"),
        workflow_dir=bundled_workflow_dir("default"))
    assert "spec-writer" not in out
    assert "spec-finalizer" in out


def test_threshold_bare_open_keeps_concierge_and_scout_hats(tmp_path) -> None:
    # Regression guard: the moved unseeded-open prose must NOT force a build-only
    # question or a no-file-read freeze on the bare (unseeded) open.
    body = _wf().agents["threshold"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    out = render_prompt(body, build_context(
        quest_title="t", event_log=log, quest_id="abc",
        quest_home=".imp/quests/abc"),
        workflow_dir=bundled_workflow_dir("default"))
    low = out.lower()
    assert "do not read any files, do not explore the codebase" not in low
    assert "what the user wants to build" not in low
    assert "take stock of existing work" in low
    assert "their reply picks the hat" in low


def test_express_path_skips_spec_beat_full_path_keeps_it() -> None:
    wf = _wf()
    steps = _steps(wf)
    # Both ceremonies enter the scope-code-check gate first.
    express = evaluate_rules(steps["threshold"].rules, vars=_BASE_VARS,
                             output={"status": "Success", "ceremony": "express"})
    assert isinstance(express.action, Goto) and express.action.target == "scope-code-check"
    full = evaluate_rules(steps["threshold"].rules, vars=_BASE_VARS,
                          output={"status": "Success", "ceremony": "full"})
    assert isinstance(full.action, Goto) and full.action.target == "scope-code-check"
    # On a clean tree the gate forwards by fast_path: express -> spec-written (the
    # draft-spec presence gate), full -> spec-finalizer (the first chromed beat).
    gate = steps["scope-code-check"].rules
    gate_express = evaluate_rules(gate, vars={**_BASE_VARS, "fast_path": True},
                                  output={"exit": 0, "stdout": "", "stderr": ""})
    assert isinstance(gate_express.action, Goto) and gate_express.action.target == "spec-written"
    gate_full = evaluate_rules(gate, vars={**_BASE_VARS, "fast_path": False},
                               output={"exit": 0, "stdout": "", "stderr": ""})
    assert isinstance(gate_full.action, Goto) and gate_full.action.target == "spec-finalizer"
    # spec-written's clean exit is the express hand-off to plan-writer (spec phase bypassed).
    written = evaluate_rules(steps["spec-written"].rules, vars={**_BASE_VARS, "fast_path": True},
                             output={"exit": 0, "stdout": "", "stderr": ""})
    assert isinstance(written.action, Goto) and written.action.target == "plan-writer"
    # The spec phase is still the second phase, entered only on the full path.
    assert [p.name for p in wf.phases][:2] == ["threshold", "spec"]


def test_threshold_routes_inline_satisfiable_requests_to_bare_turn_end_yield() -> None:
    fd = FRONT_DOOR.read_text()
    body = _wf().agents["threshold"].prompt_template_source
    low = fd.lower()
    assert "text-generation ask" in low
    assert "answered in prose" in low and "end your turn to yield" in fd
    assert "wait_for_user" not in fd
    assert "wait_for_user" not in body
    assert "**never** `complete_step`" in fd
    # complete_step is framed as the build terminator in the threshold tail.
    assert "go-live build terminator" in body.lower()
    assert "build *quickly*" in body
    assert "no-build exit" in body.lower()


def _render_threshold(tmp_path, **ctx_kwargs) -> str:
    body = _wf().agents["threshold"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    return render_prompt(body, build_context(
        quest_title="t", event_log=log, quest_id="abc",
        quest_home=".imp/quests/abc", **ctx_kwargs),
        workflow_dir=bundled_workflow_dir("default"))


def test_threshold_surfaces_parent_only(tmp_path) -> None:
    out = _render_threshold(tmp_path, parent={"id": "dute11", "title": "Open front"})
    assert "This quest was spawned from **dute11 · Open front**." in out
    assert "This quest depends on" not in out


def test_threshold_surfaces_unmerged_dependency(tmp_path) -> None:
    out = _render_threshold(tmp_path, depends_on=[
        {"id": "tuhe55", "title": "Rewrite imp-knowledge", "merged": False}])
    assert "This quest depends on **tuhe55 · Rewrite imp-knowledge** (not yet merged)." in out
    assert "can't go live until every dependency has merged." in out


def test_threshold_surfaces_all_merged_dependencies(tmp_path) -> None:
    out = _render_threshold(tmp_path, depends_on=[
        {"id": "tuhe55", "title": "Rewrite imp-knowledge", "merged": True}])
    assert "**tuhe55 · Rewrite imp-knowledge** (merged)." in out
    assert "Clear to go live." in out


def test_threshold_surfacing_block_empty_when_no_refs(tmp_path) -> None:
    out = _render_threshold(tmp_path)
    assert "This quest was spawned from" not in out
    assert "This quest depends on" not in out


def test_threshold_live_gate_conventions_relocated_to_partial(tmp_path) -> None:
    body = _wf().agents["threshold"].prompt_template_source
    # The inline convention list is gone from threshold's own raw body...
    # (guard on the sentence's lead-in, which — unlike the line-wrapped phrase —
    # is a contiguous string that a revert would reintroduce).
    assert "Follow the live-gate" not in body
    # ...but resolves through the included partial chain in the rendered prompt.
    log = JsonlEventLog(tmp_path / "events.jsonl")
    out = render_prompt(body, build_context(
        quest_title="t", event_log=log, quest_id="abc",
        quest_home=".imp/quests/abc"),
        workflow_dir=bundled_workflow_dir("default"))
    assert "no co-equal dangling alternative" in out


def test_threshold_base_line_single_clause_when_checkout_matches(tmp_path) -> None:
    out = _render_threshold(tmp_path, base_branch="main", checkout_branch="main")
    assert "This quest forks from **main** at go-live." in out
    assert "the main checkout is on" not in out


def test_threshold_base_line_names_both_branches_on_divergence(tmp_path) -> None:
    out = _render_threshold(
        tmp_path, base_branch="release-2.0", checkout_branch="master")
    assert (
        "This quest forks from **release-2.0** at go-live, but the main checkout is on "
        "**master** — what you read here may not match the fork point; check against the "
        "base with `git show release-2.0:<path>` when it matters."
    ) in out


def test_threshold_base_line_single_clause_when_checkout_branch_empty(tmp_path) -> None:
    # Materialized quest: no divergence clause.
    out = _render_threshold(tmp_path, base_branch="release-2.0")
    assert "This quest forks from **release-2.0** at go-live." in out
    assert "the main checkout is on" not in out


def test_threshold_base_line_names_a_detached_main_checkout(tmp_path) -> None:
    out = _render_threshold(
        tmp_path, base_branch="master", checkout_branch="a detached HEAD")
    assert (
        "This quest forks from **master** at go-live, but the main checkout is on "
        "**a detached HEAD** — what you read here may not match the fork point; check "
        "against the base with `git show master:<path>` when it matters."
    ) in out


def test_threshold_base_line_absent_without_a_base(tmp_path) -> None:
    # No base in the namespace -> no state line at all (the section's own
    # "forks from its base branch" sentence is unconditional and stays).
    out = _render_threshold(tmp_path)
    assert "This quest forks from **" not in out


def test_threshold_renders_base_change_section_with_quest_id(tmp_path) -> None:
    out = _render_threshold(tmp_path, base_branch="main", checkout_branch="main")
    assert "## Changing the base branch" in out
    assert "imp edit abc --base <branch>" in out
    assert "imp edit abc --depends-on` / `--base`" in out

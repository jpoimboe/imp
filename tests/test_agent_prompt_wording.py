from __future__ import annotations

import pathlib

from imp.agents.prompts import build_context, render_prompt
from imp.assets import bundled_workflow_dir
from imp.engine.loader import load_workflow
from imp.engine.state import JsonlEventLog


def _agent_prompt(name: str) -> str:
    wf = load_workflow(bundled_workflow_dir("imp"))
    return wf.agents[name].prompt_template_source


def test_finalize_prompt_has_plain_language_guidance() -> None:
    body = _agent_prompt("finalize")
    assert "never surface internal gate jargon" in body
    assert "own diff" in body


def test_finalize_prompt_avoids_full_width_rules() -> None:
    body = _agent_prompt("finalize")
    assert "do **not** draw full-width horizontal rules" in body


def test_spec_finalizer_writes_to_artifacts_no_commit_script() -> None:
    body = _agent_prompt("spec-finalizer")
    assert "{{ change.home }}/artifacts/spec/" in body
    assert "imp artifact-commit" not in body
    # The old code-branch path and git-commit machinery are gone.
    assert "imp/specs/" not in body
    assert "git commit --amend" not in body


def test_no_agent_or_partial_names_the_old_spec_writer() -> None:
    import pathlib
    base = pathlib.Path("src/imp/assets/workflows/imp")
    for sub in ("agents", "partials"):
        for p in sorted((base / sub).glob("*.md")):
            assert "spec-writer" not in p.read_text(), \
                f"{p.name} still names spec-writer"


def test_cli_new_help_does_not_name_the_old_spec_writer() -> None:
    import pathlib
    text = pathlib.Path("src/imp/cli/main.py").read_text()
    assert "handed verbatim to the spec-writer" not in text
    assert "the spec-writer names the Change" not in text
    assert "spec-writer's opening intent" not in text
    assert "the spec-writer opens" not in text


def test_plan_writer_writes_to_artifacts_no_commit_script() -> None:
    body = _agent_prompt("plan-writer")
    assert "{{ change.home }}/artifacts/plans/" in body
    assert "imp artifact-commit" not in body
    # No plan fixup / amend machinery survives.
    assert "--fixup" not in body
    assert "git commit --amend" not in body
    assert "imp/plans/" not in body


def test_plan_writer_decides_and_records_scope_contradictions() -> None:
    body = _agent_prompt("plan-writer")
    assert "correction-with-a-default" in body
    assert "marginal cost" in body
    assert "ships a broken feature" in body
    assert "ride to the" in body
    # Stale "authoritative for that point" phrase is reconciled away.
    assert "authoritative for that point" not in body


def test_both_prompts_route_recorded_scope_deviations_to_the_gate() -> None:
    writer = _agent_prompt("plan-writer")
    reviewer = _agent_prompt("plan-reviewer")
    # No contradiction between the two: both route a recorded scope deviation to
    # the merge gate rather than re-litigating it in the loop.
    assert "ride to the" in writer
    assert "ride to the" in reviewer


def test_no_agent_prompt_references_code_branch_artifact_paths() -> None:
    wf = load_workflow(bundled_workflow_dir("imp"))
    for name, agent in wf.agents.items():
        body = agent.prompt_template_source
        assert "imp/specs/" not in body, f"{name} still references imp/specs/"
        assert "imp/plans/" not in body, f"{name} still references imp/plans/"


def test_batch_code_reviewer_has_no_ambient_head_in_diff_ranges() -> None:
    body = _agent_prompt("batch-code-reviewer")
    assert "..HEAD" not in body
    assert body.count("..imp/{{ change.id }}") == 1


def test_batch_code_reviewer_renders_change_diff(tmp_path) -> None:
    body = _agent_prompt("batch-code-reviewer")
    log = JsonlEventLog(tmp_path / "events.jsonl")
    out = render_prompt(body, build_context(
        quest_title="t", event_log=log, quest_id="abc",
        quest_home=".imp/quests/abc", cur_base_head="BASE",
        vars={"fast_path": False}),
        workflow_dir=bundled_workflow_dir("imp"))
    assert "git diff BASE..imp/abc" in out
    assert "..HEAD" not in out


def test_plan_writer_carries_illegal_states_steer() -> None:
    body = _agent_prompt("plan-writer")
    assert (
        "Make illegal states unrepresentable — prefer a design where the type or "
        "structure rules out the bad case over one that admits it and then validates "
        "or falls back."
    ) in body


def test_code_reviewer_flags_defensive_sprawl() -> None:
    body = _agent_prompt("code-reviewer")
    assert "Defensive handling of states that can't occur, fallbacks that mask a bad state" in body
    assert "Simpler-and-stricter beats tolerant-and-complex." in body


def test_persona_designs_bad_states_out_before_skipping_guards() -> None:
    text = pathlib.Path(
        "src/imp/assets/workflows/imp/persona.md"
    ).read_text()
    assert "Design so bad states can't arise; then don't add error handling, fallbacks," in text
    # The remainder of the rule stays intact.
    assert "Only validate at system boundaries (user input," in text
    # The old bare opener is gone.
    assert "- Don't add error handling, fallbacks, or validation for scenarios" not in text


def test_no_workflow_prompt_invokes_artifact_commit() -> None:
    for wf_name in ("imp",):
        wf = load_workflow(bundled_workflow_dir(wf_name))
        for name, agent in wf.agents.items():
            body = agent.prompt_template_source
            assert "imp artifact-commit" not in body, \
                f"{wf_name}/{name} still instructs imp artifact-commit"
            assert "journal-wip" not in body, \
                f"{wf_name}/{name} still names the journal-wip ref"


def test_gap_check_change_review_is_unconditional() -> None:
    body = _agent_prompt("gap-check")
    assert "if the change spanned more than one plan" not in body
    assert "once-only change-wide review." in body


def test_batch_code_reviewer_description_is_whole_diff() -> None:
    from imp.engine.loader import parse_agent_file
    desc = parse_agent_file(
        pathlib.Path("src/imp/assets/workflows/imp/agents/batch-code-reviewer.md").resolve()
    ).description
    assert "per-batch" not in desc
    assert "whole diff" in desc


def test_query_events_references_frame_provenance_not_current_state() -> None:
    for wf_name in ("imp",):
        wf = load_workflow(bundled_workflow_dir(wf_name))
        for name in ("summarizer", "spec-reviewer", "plan-reviewer"):
            body = wf.agents[name].prompt_template_source
            assert "query_events" in body, f"{wf_name}/{name}"
            assert "not current state" in body, f"{wf_name}/{name}"


def test_task_lead_uses_agent_id_filter_for_subagent_events() -> None:
    body = _agent_prompt("task-lead")
    assert "filter with `agent_id=sub:" in body
    assert "logged under `agent_id=sub:" not in body


def test_patent_writer_resume_prose_finishes_with_complete_step() -> None:
    # The six named patent writers must tell a resumed agent to FINISH the turn
    # with complete_step, not the vague/handoff-implying "and re-emit." tail. The
    # assertion targets the handoff phrase "and re-emit." rather than the bare
    # substring "re-emit", because assemble.md legitimately says "your own pre-emit
    # pass" (which CONTAINS "re-emit") in unrelated, out-of-scope prose (line 57) —
    # a bare `"re-emit" not in body` check would false-fail on assemble forever.
    base = pathlib.Path("src/imp/assets/workflows/patent/agents")
    for name in ("design-spec", "claims", "spec-body", "figures",
                 "independent-claims", "assemble"):
        body = (base / f"{name}.md").read_text()
        assert "and re-emit." not in body, \
            f"{name} still ends its resume sentence with 'and re-emit.'"
        assert "finish the same turn with `complete_step`" in body, name


def test_named_resume_agents_carry_no_handoff_reemit_prose() -> None:
    # Regression guard: imp `reconciler` is named in the AC but already ends
    # firmly on complete_step with no "re-emit"/handoff resume sentence. Lock
    # that so a future edit cannot reintroduce the pattern.
    imp_reconciler = pathlib.Path(
        "src/imp/assets/workflows/imp/agents/reconciler.md").read_text()
    assert "re-emit" not in imp_reconciler
    assert "complete_step" in imp_reconciler


def test_threshold_renames_early_on_design_formation() -> None:
    body = _agent_prompt("threshold")
    # Rename fires as soon as a design forms, not deferred to go-live.
    assert "as soon as a design has formed" in body
    # Pure concierge/scout conversations are explicitly excluded from renaming.
    assert "so they never rename" in body
    # The go-live rename survives as a fallback for straight-to-build.
    assert "a fallback for when you went" in body


def test_spec_reviewer_blocks_on_scope_ac_cross_walk() -> None:
    body = _agent_prompt("spec-reviewer")
    # the cross-walk is a review check, run in both directions
    assert "Scope↔AC cross-walk in both directions" in body
    # a broad AC over a narrow Scope is a blocking Major inconsistency
    assert "A broad AC over a narrow Scope" in body
    assert "blocking inconsistency (Major)" in body


def test_spec_finalizer_requires_checkable_ac_clauses_and_cross_walk() -> None:
    body = _agent_prompt("spec-finalizer")
    # independently-checkable clauses; compound criteria split or sub-lettered
    assert "independently checkable clauses" in body
    assert "sub-lettered" in body
    # every AC clause names / maps to its delivering Scope item
    assert "Scope item that delivers" in body
    # both-directions cross-walk before finalizing
    assert "cross-walk Scope↔AC in **both directions**" in body
    # a too-narrow Scope is reconciled, not treated as a wrong AC
    assert "Scope section is too narrow" in body
    # the threshold-handoff checklist references the cross-walk so it fires on
    # the standard adopt path (phrase appears in both the checklist and the section)
    assert body.count("cross-walk Scope↔AC") >= 2


def test_plan_writer_documents_untagged_scope_out_form() -> None:
    body = _agent_prompt("plan-writer")
    # scheduling a spec-mandated item into a later plan is legitimate
    assert "Slicing spec-mandated work into a later plan is legitimate" in body
    # the whether is never in play, only the when
    assert "the **whether** is never in play, only the **when**" in body
    # the required destination path, unique to this new section
    assert (
        "append the plain **untagged** DEFERRED line to `{{ change.home }}/deferred.md`"
        in body
    )
    # the plain untagged form (no needs-decision marker), citing the spec ref
    assert (
        "DEFERRED plan-writer: <item, citing the spec AC/scope ref> [scope: change]"
        in body
    )
    # prose in the Decisions section does not count — only the untagged line records it
    assert "Prose in the plan's `## Decisions` section does not count" in body


def test_plan_reviewer_backstops_unrecorded_scope_out() -> None:
    body = _agent_prompt("plan-reviewer")
    # the blanket read-only clause is reconciled to a deferred.md-only exception
    assert "You are read-only with a single exception" in body
    assert "You are read-only: do not write or modify any files." not in body
    # the destination path, unique to the new carve-out clause
    assert (
        "the sole file you may write is `{{ change.home }}/deferred.md`"
        in body
    )
    # the scope-out checklist item
    assert "Scope-out recorded" in body
    assert "assigned to a later plan by the spec's `## Plan outline`" in body
    # the backstop write authority: append the untagged line itself, source plan-reviewer
    assert "unrecorded scope-out" in body
    assert (
        "DEFERRED plan-reviewer: <item, citing the spec AC/scope ref> [scope: change]"
        in body
    )


def test_gap_check_walks_acceptance_criteria_clause_by_clause() -> None:
    body = _agent_prompt("gap-check")
    # a dedicated AC-walk section, splitting compound criteria into checkable clauses
    assert "Walk the acceptance criteria clause by clause" in body
    assert "independently checkable clauses" in body
    # verify every clause against committed code, at the production site not the compute site
    assert "against the committed code" in body
    assert "the line that emits or renders the behavior, not the line that computes it" in body
    # deferred.md is corroboration only
    assert "read `deferred.md` as corroboration only" in body
    # dispositions: needs-decision parks/suppresses; any other unmet clause drives more_plans
    assert "does not drive a plan" in body
    # the unparked-unmet disposition drives a corrective plan (unique to the new bullet)
    assert "corrective plan that covers it" in body
    # sole lever unchanged: never writes deferred.md
    assert "read `deferred.md` and never write it" in body


def test_deferred_sweep_protects_spec_cited_untagged_items() -> None:
    body = _agent_prompt("deferred-sweep")
    # (a) the source note names plan-writer and plan-reviewer as originators
    assert "plan-writer, and plan-reviewer append it" in body
    # (b) a spec-cited untagged item may be fixed or escalated, never dismissed
    assert "cites spec scope" in body
    assert "never dismissed" in body
    # (c) an item already landed by a corrective plan is closed fixed, not dismissed
    assert "already landed by a corrective plan" in body
    assert "close it `— fixed <sha>`" in body


def test_finalize_cover_check_marks_covered_by() -> None:
    body = _agent_prompt("finalize")
    low = body.lower()
    # The cover-check consumes imp search's terse -l list across active + backlog.
    assert "imp search --active --backlog -l" in body
    # A confirmed cover writes the covered-by terminal marker.
    assert "— covered-by <id>" in body
    # Fail-safe: only a positively confirmed tracker suppresses the human ask.
    assert "surface, never suppress" in low
    assert "positively confirmed" in low


def _render_agent(name: str, tmp_path) -> str:
    body = _agent_prompt(name)
    log = JsonlEventLog(tmp_path / f"{name}-events.jsonl")
    return render_prompt(
        body,
        build_context(
            quest_title="t",
            event_log=log,
            quest_id="abc",
            quest_home=".imp/quests/abc",
            cur_base_head="BASE",
            vars={"fast_path": False, "task_total": 0},
        ),
        workflow_dir=bundled_workflow_dir("imp"),
    )


def test_synced_pair_notes_stripped_from_rendered_prompts(tmp_path) -> None:
    # The maintainer-only SYNCED PAIR coordination note must not reach the model.
    for name in ("task-lead", "deferred-sweep"):
        out = _render_agent(name, tmp_path)
        assert "SYNCED PAIR" not in out, name
        # The Jinja comment delimiters must not leak into the rendered body either.
        assert "{#" not in out and "#}" not in out, name

    # The SYNCED PAIR blocks in source now use Jinja comment delimiters, not HTML.
    for name in ("task-lead", "deferred-sweep"):
        src = _agent_prompt(name)
        assert "<!-- SYNCED PAIR" not in src, name
        assert "{# SYNCED PAIR" in src, name

    # Regression: plan-writer.md's intentional inline example comment is untouched.
    assert "<!-- HARD tasks ONLY" in _agent_prompt("plan-writer")

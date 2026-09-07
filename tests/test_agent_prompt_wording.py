from __future__ import annotations

import pathlib

from imp.agents.prompts import build_context, render_prompt
from imp.assets import bundled_workflow_dir
from imp.engine.loader import load_workflow
from imp.engine.state import JsonlEventLog


def _agent_prompt(name: str) -> str:
    wf = load_workflow(bundled_workflow_dir("default"))
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
    assert "{{ quest.home }}/artifacts/spec/" in body
    assert "imp artifact-commit" not in body
    # The old code-branch path and git-commit machinery are gone.
    assert "imp/specs/" not in body
    assert "git commit --amend" not in body


def test_no_agent_or_partial_names_the_old_spec_writer() -> None:
    import pathlib
    base = pathlib.Path("src/imp/assets/workflows/default")
    dirs = [base / "agents", base / "partials", pathlib.Path("src/imp/assets/partials")]
    for d in dirs:
        for p in sorted(d.glob("*.md")):
            assert "spec-writer" not in p.read_text(), \
                f"{p.name} still names spec-writer"


def test_cli_new_help_does_not_name_the_old_spec_writer() -> None:
    import pathlib
    text = pathlib.Path("src/imp/cli/main.py").read_text()
    assert "handed verbatim to the spec-writer" not in text
    assert "the spec-writer names the Quest" not in text
    assert "spec-writer's opening intent" not in text
    assert "the spec-writer opens" not in text


def test_plan_writer_writes_to_artifacts_no_commit_script() -> None:
    body = _agent_prompt("plan-writer")
    assert "{{ quest.home }}/artifacts/plans/" in body
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
    wf = load_workflow(bundled_workflow_dir("default"))
    for name, agent in wf.agents.items():
        body = agent.prompt_template_source
        assert "imp/specs/" not in body, f"{name} still references imp/specs/"
        assert "imp/plans/" not in body, f"{name} still references imp/plans/"


def test_batch_code_reviewer_has_no_ambient_head_in_diff_ranges() -> None:
    body = _agent_prompt("batch-code-reviewer")
    assert "..HEAD" not in body
    assert body.count("..imp/{{ quest.id }}") == 1


def test_batch_code_reviewer_renders_quest_diff(tmp_path) -> None:
    body = _agent_prompt("batch-code-reviewer")
    log = JsonlEventLog(tmp_path / "events.jsonl")
    out = render_prompt(body, build_context(
        quest_title="t", event_log=log, quest_id="abc",
        quest_home=".imp/quests/abc", cur_base_head="BASE",
        vars={"fast_path": False}),
        workflow_dir=bundled_workflow_dir("default"))
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
        "src/imp/assets/partials/coding-stance.md"
    ).read_text()
    assert "Design so bad states can't arise; then add no error handling, fallbacks," in text
    # The remainder of the rule stays intact.
    assert "validate only at system boundaries (user input," in text
    # The old bare opener is gone.
    assert "- Don't add error handling, fallbacks, or validation for scenarios" not in text


def test_no_workflow_prompt_invokes_artifact_commit() -> None:
    for wf_name in ("default",):
        wf = load_workflow(bundled_workflow_dir(wf_name))
        for name, agent in wf.agents.items():
            body = agent.prompt_template_source
            assert "imp artifact-commit" not in body, \
                f"{wf_name}/{name} still instructs imp artifact-commit"
            assert "journal-wip" not in body, \
                f"{wf_name}/{name} still names the journal-wip ref"


def test_gap_check_quest_review_is_unconditional() -> None:
    body = _agent_prompt("gap-check")
    assert "if the change spanned more than one plan" not in body
    assert "once-only quest-wide review." in body


def test_batch_code_reviewer_description_is_whole_diff() -> None:
    from imp.engine.loader import parse_agent_file
    desc = parse_agent_file(
        pathlib.Path("src/imp/assets/workflows/default/agents/batch-code-reviewer.md").resolve()
    ).description
    assert "per-batch" not in desc
    assert "whole diff" in desc


def test_events_query_references_frame_provenance_not_current_state() -> None:
    for wf_name in ("default",):
        wf = load_workflow(bundled_workflow_dir(wf_name))
        for name in ("summarizer", "plan-reviewer"):
            body = wf.agents[name].prompt_template_source
            assert "imp events" in body, f"{wf_name}/{name}"
            assert "not current state" in body, f"{wf_name}/{name}"


def test_task_lead_uses_agent_selector_for_subagent_events() -> None:
    body = _agent_prompt("task-lead")
    assert "--agent sub:" in body


def test_named_resume_agents_carry_no_handoff_reemit_prose() -> None:
    # Regression guard: imp `reconciler` is named in the AC but already ends
    # firmly on complete_step with no "re-emit"/handoff resume sentence. Lock
    # that so a future edit cannot reintroduce the pattern.
    imp_reconciler = pathlib.Path(
        "src/imp/assets/workflows/default/agents/reconciler.md").read_text()
    assert "re-emit" not in imp_reconciler
    assert "complete_step" in imp_reconciler


def test_threshold_renames_early_on_design_formation() -> None:
    fd = pathlib.Path("src/imp/assets/partials/front-door.md").read_text()
    body = _agent_prompt("threshold")
    # Rename fires as soon as a design forms, not deferred to go-live.
    assert "as soon as a design has formed" in fd
    # Pure concierge/scout conversations are explicitly excluded from renaming.
    assert "so they never rename" in fd
    # The go-live rename survives as a fallback for straight-to-build (threshold tail).
    assert "a fallback for when you went" in body


def test_spec_reviewer_blocks_on_scope_ac_cross_walk() -> None:
    body = _agent_prompt("spec-reviewer")
    # the cross-walk is a review check, run in both directions
    assert "Scope↔AC cross-walk in both directions" in body
    # a broad AC over a narrow Scope is a blocking Major inconsistency
    assert "A broad AC over a narrow Scope" in body
    assert "blocking inconsistency (Major)" in body


def test_spec_reviewer_verifies_forward_claims() -> None:
    body = _agent_prompt("spec-reviewer")
    # the new How-to-Review bullet checks forward claims, not only backward ones
    assert "Verify the spec's forward claims, not only its backward ones." in body
    # a correct citation is not the same as a fit-for-purpose one
    assert "A citation being *correct* is not the same as its being *fit for the job*." in body
    # timing/lifetime correctness is an empirical unknown to trace, never assume
    assert (
        "an empirical unknown to trace explicitly,\n  never to assume from a static reading."
        in body
    )
    # the symmetric Calibration paragraph classes an accurate-but-unfit seam as Major
    assert (
        "an *accurate* reference to a seam that does not deliver the\n"
        "behavior the spec describes is **Major** too"
        in body
    )


def test_spec_reviewer_checks_threshold_decision_fidelity() -> None:
    body = _agent_prompt("spec-reviewer")
    # the new How-to-Review bullet mandates a transcript-fidelity check
    assert "Keep faith with what was decided." in body
    # it reads the clean threshold dialogue via imp log --step threshold
    assert "imp log {{ quest.id }} --step threshold" in body
    # the new Calibration paragraph classes a dropped/reversed decision as Major
    assert "silently drops or contradicts is **Major**" in body
    assert (
        "recorded dialogue, not your assumption of what was decided." in body
    )
    # the retarget removes imp events from spec-reviewer entirely
    assert "imp events" not in body


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
        "append the plain **untagged** DEFERRED line to `{{ quest.home }}/deferred.md`"
        in body
    )
    # the plain untagged form (no needs-decision marker), citing the spec ref
    assert (
        "DEFERRED plan-writer: <item, citing the spec AC/scope ref> [scope: quest]"
        in body
    )
    # prose in the Decisions section does not count — only the untagged line records it
    assert "Prose in the plan's `## Decisions` section does not count" in body


def test_plan_reviewer_backstops_unrecorded_scope_out() -> None:
    body = _agent_prompt("plan-reviewer")
    # the blanket read-only clause is reconciled to a deferred.md-only exception
    assert "You may write exactly one file" in body
    assert "You are read-only: do not write or modify any files." not in body
    # the destination path, unique to the new carve-out clause
    assert "`{{ quest.home }}/deferred.md`, to append an untagged scope-out line" in body
    # the scope-out checklist item
    assert "Scope-out recorded" in body
    assert "assigned to a later plan by the spec's `## Plan outline`" in body
    # the backstop write authority: append the untagged line itself, source plan-reviewer
    assert "unrecorded scope-out" in body
    assert (
        "DEFERRED plan-reviewer: <item, citing the spec AC/scope ref> [scope: quest]"
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


def test_finalize_already_tracked_check_marks_tracked_by() -> None:
    body = _agent_prompt("finalize")
    low = body.lower()
    # The already-tracked check consumes imp search's terse -l list across active + backlog.
    assert "imp search --active --backlog -l" in body
    # §1a also searches the merged-after-fork archive subset, fork-point gated.
    assert (
        "imp search --active --backlog "
        "--merged-after {{ quest.orig_base_head }} -l" in body
    )
    # A Jinja guard keeps the completed dimension only when a fork point exists;
    # the plain active+backlog search is the no-fork-point fallback.
    assert "{% if quest.orig_base_head %}" in body
    # The §1a framing names completed quests alongside active and backlog.
    assert "active, backlog, or completed" in body
    # A confirmed tracker writes the tracked-by terminal marker.
    assert "— tracked-by <id>" in body
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
        workflow_dir=bundled_workflow_dir("default"),
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


def test_spec_finalizer_teaches_green_per_plan_slicing() -> None:
    body = " ".join(_agent_prompt("spec-finalizer").split())
    assert (
        "**Each plan must land the tree green.** The `verify` gate runs the "
        "project's fast suite after *every* plan, not once at the end — so no "
        "plan may defer greening to a later one. Slice **end-to-end** (carry "
        "one unit through emit, readers, and tests together), never in "
        "**horizontal layers** that leave the suite red between plans (e.g. "
        "rename an API in plan 1, fix its consumers in a later plan). If scope "
        "genuinely can't be sliced green, raise it as a `— needs-decision:` "
        "item rather than bake a red tree into the outline."
    ) in body


def test_plan_writer_consume_outline_notes_green_this_plan() -> None:
    body = " ".join(_agent_prompt("plan-writer").split())
    assert (
        "The `verify` gate runs the project's fast suite after **this** plan "
        "(and every plan) — so your tasks must leave the tree green at this "
        "plan's end; never defer greening to a later plan. If the outline "
        "slice you were handed can't land green, that's an outline defect: "
        "flag it as a `— needs-decision:` item, don't plan a red tree."
    ) in body


def test_plan_writer_gate_phrases_are_per_plan() -> None:
    body = " ".join(_agent_prompt("plan-writer").split())
    # No Redundant Whole-Project Gate Task section
    assert (
        "gate runs the project's `test_cmd_fast` after **each plan's** "
        "implement phase and routes any failure"
    ) in body
    # Self-Review item 5 (Lint vs. test scope)
    assert (
        "runs after **each plan's** implement phase and routes back on any "
        "failure"
    ) in body
    # both ambiguous "after the implement phase" phrasings are gone from this prompt
    assert "after the implement phase" not in body


def test_plan_reviewer_flags_red_leaving_plan_as_major() -> None:
    body = " ".join(_agent_prompt("plan-reviewer").split())
    assert (
        "- **Green at this plan's end** — the prescribed code leaves the fast "
        "suite green. A plan that defers greening to a later plan (renames or "
        "removes something and leaves its consumers or tests red) is a "
        "**Major** finding: the `verify` gate runs after every plan and will "
        "reject it."
    ) in body


def test_spec_finalizer_adopts_grounded_draft_not_reground(tmp_path) -> None:
    from imp.engine.loader import parse_agent_file
    path = pathlib.Path(
        "src/imp/assets/workflows/default/agents/spec-finalizer.md"
    ).resolve()
    full = path.read_text()
    agent = parse_agent_file(path)
    body = agent.prompt_template_source
    desc = agent.description
    flat = " ".join(body.split())
    # (a) The moved-base conditional and both persisted-head vars are gone from
    # the whole file (mirrors the AC grep over spec-finalizer.md).
    assert "cur_base_head" not in full
    assert "orig_base_head" not in full
    assert "{% if quest.cur_base_head" not in full
    # (a2) No re-grounding language survives anywhere in the file (frontmatter
    # description OR prompt body).
    assert "re-ground" not in full
    # (a2) The frontmatter description now describes adopting the grounded draft.
    # It lives in frontmatter, so read it via parse_agent_file().description —
    # prompt_template_source (body) excludes the frontmatter.
    assert "already grounded against the current base at go-live" in desc
    # (a2/b) The Your Role summary and the handoff (both body-resident) describe
    # adopting the grounded draft.
    assert "adopt the already-grounded draft as-is" in flat
    assert "already grounded against the current base tip at go-live" in flat
    assert "**Adopt the draft as-is**" in flat
    # (b) The surrounding adopt directives are preserved.
    assert "**adopt and finalize** it, do not re-author" in flat
    assert (
        "Skip the opening question, do not call `create_and_enter_worktree`, "
        "and do not run the build-or-save gate" in flat
    )
    # (c) The parking escape hatch is still referenced from the handoff.
    assert "Surfacing a decision you cannot resolve" in body
    # (f) The template still renders without error.
    out = _render_agent("spec-finalizer", tmp_path)
    assert "Adopt the draft as-is" in out


def test_threshold_closes_superseded_backlog_at_approval(tmp_path) -> None:
    fd = pathlib.Path("src/imp/assets/partials/front-door.md").read_text()

    # The section exists with its exact heading, placed AFTER the depends-on
    # section and BEFORE "## Acting on imp".
    heading = "## Close redundant backlog quests at approval"
    assert heading in fd
    depends = fd.index("## When the work depends on another quest")
    acting = fd.index("## Acting on imp")
    here = fd.index(heading)
    assert depends < here < acting

    # Verbatim load-bearing phrases from the agreed copy.
    assert "clearly supersedes existing **backlog** quests" in fd
    assert "carry them into the go-live proposal: approving the build closes them too" in fd
    assert '`imp abandon <id>` each; say plainly the close is permanent' in fd
    assert 'Don\'t defer it to a "close at merge" note in the spec' in fd
    assert "A superseded **in-flight** quest you flag" in fd
    assert "never close this way" in fd

    # NOT autonomy-wrapped: rendering the threshold with autonomous=True strips
    # every `{% if not run.autonomous %}` block, yet this section must survive.
    body = _agent_prompt("threshold")
    log = JsonlEventLog(tmp_path / "threshold-events.jsonl")
    rendered = render_prompt(
        body,
        build_context(
            quest_title="t",
            event_log=log,
            quest_id="abc",
            quest_home=".imp/quests/abc",
            autonomous=True,
            vars={"fast_path": False},
        ),
        workflow_dir=bundled_workflow_dir("default"),
    )
    assert heading in rendered
    # Sanity: the autonomy-guarded section IS stripped in the same render, so a
    # surviving heading genuinely proves the new section is unwrapped.
    assert "## Keep discovered work here by default" not in rendered


_CALIBRATION_SENTENCE = (
    "An area that returns lap after lap means the design is unsettled, not the spec "
    "under-written: say so in `findings` — another Major buys another lap, not a "
    "decision — and don't manufacture minors to justify one more pass; on such a "
    "review `has_minor_findings = false` is the honest answer."
)


def _render_spec_reviewer(tmp_path, prior_laps=None) -> str:
    log = JsonlEventLog(tmp_path / "spec-reviewer-events.jsonl")
    return render_prompt(
        _agent_prompt("spec-reviewer"),
        build_context(
            quest_title="t", event_log=log, quest_id="abc",
            quest_home=".imp/quests/abc",
            vars={"fast_path": False, "task_total": 0},
            prior_laps=prior_laps,
        ),
        workflow_dir=bundled_workflow_dir("default"),
    )


def test_spec_reviewer_first_lap_names_lap_one_and_omits_history(tmp_path) -> None:
    out = _render_spec_reviewer(tmp_path)
    assert "This is review lap 1." in out
    assert "Prior laps ended" not in out          # no clause, no empty-list artifact
    assert "This is review lap ." not in out


def test_spec_reviewer_later_lap_lists_outcomes_and_no_findings(tmp_path) -> None:
    out = _render_spec_reviewer(tmp_path, prior_laps=[
        {"lap": 1, "outcome": "reworked", "head": "aaa1111",
         "findings": "MUST-NOT-LEAK"},
        {"lap": 2, "outcome": "minors integrated", "head": "bbb2222"},
    ])
    assert "This is review lap 3." in out
    assert "Prior laps ended — 1: reworked; 2: minors integrated." in out
    assert "MUST-NOT-LEAK" not in out


def test_spec_reviewer_reads_the_quest_spike_dir(tmp_path) -> None:
    out = _render_spec_reviewer(tmp_path)
    assert ".imp/quests/abc/artifacts/spike/" in out
    assert "{{ quest.home }}" not in out


def test_spec_reviewer_calibration_carries_the_unsettled_design_sentence() -> None:
    body = _agent_prompt("spec-reviewer")
    assert _CALIBRATION_SENTENCE in body
    assert body.index("## Calibration") < body.index(_CALIBRATION_SENTENCE)
    assert body.index(_CALIBRATION_SENTENCE) < body.index("## Output")


def test_spec_reviewer_output_schema_stays_the_four_fields() -> None:
    wf = load_workflow(bundled_workflow_dir("default"))
    fields = wf.agents["spec-reviewer"].output_schema.fields
    assert [f.name for f in fields] == [
        "status", "findings", "blocking", "has_minor_findings"]
    text = (bundled_workflow_dir("default")
            / "agents" / "spec-reviewer.md").read_text()
    assert (
        "output:\n"
        "  status: {type: enum, values: [Success, Error]}\n"
        "  findings: {type: string}\n"
        "  blocking: {type: bool}\n"
        "  has_minor_findings: {type: bool}\n"
        "---\n"
    ) in text


def _render_plan_reviewer(tmp_path, prior_laps=None) -> str:
    log = JsonlEventLog(tmp_path / "plan-reviewer-events.jsonl")
    return render_prompt(
        _agent_prompt("plan-reviewer"),
        build_context(
            quest_title="t", event_log=log, quest_id="abc",
            quest_home=".imp/quests/abc",
            vars={"fast_path": False, "task_total": 0},
            prior_laps=prior_laps,
        ),
        workflow_dir=bundled_workflow_dir("default"),
    )


def test_plan_reviewer_first_lap_names_lap_one_and_omits_recovery(tmp_path) -> None:
    out = _render_plan_reviewer(tmp_path)
    # the whole earlier-laps body collapses to the one sentence, with no
    # leftover whitespace from the stripped {% if %}
    assert ("## Earlier laps\n\nThis is review lap 1.\n\n"
            "## Review the prescribed code like a diff (primary)") in out
    assert "Prior laps ended" not in out
    assert "imp events abc --step plan-reviewer" not in out
    assert "refs/imp/artifacts/abc" not in out
    assert "floor, not a ceiling" not in out


def test_plan_reviewer_later_lap_carries_the_recovery_protocol(tmp_path) -> None:
    out = _render_plan_reviewer(tmp_path, prior_laps=[
        {"lap": 1, "outcome": "reworked", "head": "aaaa1111bbbb2222"},
        {"lap": 2, "outcome": "minors integrated", "head": "cccc3333dddd4444"},
    ])
    # (a) per-lap outcome list, each stamped with its recorded head (8 chars)
    assert "This is review lap 3." in out
    assert ("Prior laps ended — 1: reworked (at aaaa1111); "
            "2: minors integrated (at cccc3333).") in out
    # (b) the findings-recovery command
    assert "imp events abc --step plan-reviewer --type agent_result --json" in out
    # (c) both artifact-ref commands, with P as the tree-root path, not quest.home
    assert 'P=plans/abc-plan-<N>.md' in out
    assert "not its `.imp/quests/abc` path, which matches nothing" in out
    # the assignment ships INSIDE the block: each bash call is a fresh shell, so a
    # block that only referenced $P would expand to an empty pathspec and abort
    assert ('  P=plans/abc-plan-<N>.md\n'
            '  git -C "$IMP_PROJECT_ROOT" log --oneline refs/imp/artifacts/abc'
            ' -- "$P"\n'
            '  git -C "$IMP_PROJECT_ROOT" diff <second-newest> <newest>'
            ' -- "$P"\n') in out
    # (d) effort, not judgement
    assert "This scopes **verification effort, not judgement**." in out
    # (e) the floor/ceiling calibration
    assert ("Prior findings are a floor, not a ceiling: an area no\nearlier lap "
            "mentioned is not thereby clean.") in out
    # the lap summary and the recovery instruction stay separate paragraphs
    # (trim_blocks eats the newline after the inline {% endif %} closing the lap
    # sentence, so the break has to come from inside the {% if %})
    assert ("2: minors integrated (at cccc3333).\n\n"
            'Run `git -C "$IMP_PROJECT_ROOT" rev-parse HEAD` first.') in out
    # (f) the sha rule, including the no-sha case
    assert "**One rule sorts every earlier lap:\nits sha against your HEAD.**" in out
    assert ("a lap\nstamped with a different sha — or with no sha at all — reviewed "
            "an earlier plan or an\nearlier tree; ignore it wholesale") in out
    # (g) the in-band stamp is authoritative, because the outcome list truncates
    assert "Every report ends with its\n  own `Verified at <sha>:` line" in out
    assert ("That in-band stamp is\n  authoritative: the outcome list above keeps "
            "only the last few laps") in out


def test_plan_reviewer_later_lap_renders_without_a_head_key(tmp_path) -> None:
    out = _render_plan_reviewer(tmp_path, prior_laps=[
        {"lap": 1, "outcome": "reworked"},
        {"lap": 2, "outcome": "reworked"},
    ])
    assert "This is review lap 3." in out
    line = next(ln for ln in out.splitlines() if "Prior laps ended" in ln)
    assert line == "This is review lap 3. Prior laps ended — 1: reworked; 2: reworked."
    assert " (at " not in line        # unstamped legacy entries degrade, never raise


_VERIFIED_TAIL = (
    "End `findings` with `Verified at <sha>:` (the current HEAD) and the tree facts you "
    "checked this\nlap — paths, signatures, what existing code does — each with its "
    "`file:line`. The next lap reads\nthis instead of re-opening those files; a check you "
    "don't record is a check it pays for again."
)


def test_plan_reviewer_findings_must_end_with_a_verified_facts_tail() -> None:
    body = _agent_prompt("plan-reviewer")
    assert _VERIFIED_TAIL in body
    # It sits in `## Severity & output`, right after the findings-format sentence
    # and before the blocking rule — so it is part of the output contract.
    assert body.index("## Severity & output") < body.index(_VERIFIED_TAIL)
    assert body.index("Report `findings` as classified prose") < body.index(_VERIFIED_TAIL)
    assert body.index(_VERIFIED_TAIL) < body.index("Set `blocking = true`")


def test_plan_reviewer_finish_exemplars_carry_the_verified_tail() -> None:
    body = _agent_prompt("plan-reviewer")
    # The clean exemplars are the copy a reviewer imitates last; leaving them
    # tail-less would undercut the instruction on exactly the clean lap whose
    # facts the following verification round needs.
    exemplars = body[body.index("Finish with one of:"):]
    assert (
        'complete_step(findings="No Critical/Major findings. Minor: … '
        'Verified at <sha>: …", blocking=false, has_minor_findings=true)'
    ) in exemplars
    assert (
        'complete_step(findings="No findings. Plan is complete and ready for '
        'implementation. Verified at <sha>: …", blocking=false, has_minor_findings=false)'
    ) in exemplars
    assert (
        'complete_step(findings="Critical: … / Major: … Verified at <sha>: …", '
        'blocking=true, has_minor_findings=false)'
    ) in exemplars


def test_plan_reviewer_orient_stays_intact_above_earlier_laps(tmp_path) -> None:
    out = _render_plan_reviewer(tmp_path, prior_laps=[
        {"lap": 1, "outcome": "reworked", "head": "aaaa1111bbbb2222"},
    ])
    orient = out.index("## Orient")
    earlier = out.index("## Earlier laps")
    diff_review = out.index("## Review the prescribed code like a diff (primary)")
    constraint = out.index(
        "Read exactly these two artifacts — the newest plan and the spec. Do not "
        "read prior or future\nplan files")
    deferred = out.index(
        "You may write exactly one file of record — `.imp/quests/abc/deferred.md`")
    assert orient < constraint < earlier < diff_review
    assert orient < deferred < earlier

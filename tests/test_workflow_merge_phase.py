from pathlib import Path

from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.types import Continue, Goto, Pause

WF = Path("src/imp/assets/workflows/default").resolve()


def _phase(wf, name):
    return next(p for p in wf.phases if p.name == name)


def _rules(wf, name):
    return next(s for s in wf.flat_steps() if s.name == name).rules


def test_integrate_phase_step_ordering_and_layering() -> None:
    wf = load_workflow(WF)
    names = [s.name for s in _phase(wf, "integrate").steps]
    assert names == [
        "wait-for-head", "integrate-rebase", "reconciler",
        "sync-worktree", "validate-memory", "integration-verify", "integration-reviewer",
        "check-cleared", "summarizer", "finalize",
        "cleanup-merge", "autosquash-resolve-merge",
        "assert-stamped", "merge-to-base",
    ]
    by = {s.name: s for s in _phase(wf, "integrate").steps}
    assert by["wait-for-head"].acquire == "merge-lane"
    assert by["merge-to-base"].kind == "merge_and_exit_worktree"
    assert all(s.name != "integration-resolver" for s in wf.flat_steps())


def test_integrate_rebase_routing_into_summarizer_and_gate() -> None:
    wf = load_workflow(WF)
    rules = _rules(wf, "integrate-rebase")
    m = evaluate_rules(rules, vars={}, output={"exit": 2, "stdout": ""})
    assert isinstance(m.action, Goto) and m.action.target == "summarizer"
    assert m.var_mutations == {}
    m = evaluate_rules(rules, vars={}, output={"exit": 0, "stdout": "forkSHA"})
    assert isinstance(m.action, Goto) and m.action.target == "sync-worktree"
    assert "review_base" not in m.var_mutations
    assert m.var_mutations.get("integration_fork") == "forkSHA"
    assert m.var_mutations.get("integration_review_needed") is True
    m = evaluate_rules(rules, vars={}, output={"exit": 7, "stdout": "forkSHA"})
    assert isinstance(m.action, Goto) and m.action.target == "validate-memory"
    assert m.var_mutations.get("integration_fork") == "forkSHA"
    assert m.var_mutations.get("integration_review_needed") is False
    m = evaluate_rules(rules, vars={}, output={"exit": 6, "stdout": "forkSHA"})
    assert isinstance(m.action, Continue)
    assert "review_base" not in m.var_mutations
    assert m.var_mutations.get("integration_fork") == "forkSHA"
    assert "integration_review_needed" not in m.var_mutations
    m = evaluate_rules(rules, vars={}, output={"exit": 5, "stdout": ""})
    assert isinstance(m.action, Pause)


def test_reconciler_stuck_pauses_and_else_continues() -> None:
    wf = load_workflow(WF)
    rules = _rules(wf, "reconciler")
    m = evaluate_rules(rules, vars={},
                       output={"status": "Success", "stuck": True, "summary": ""})
    assert isinstance(m.action, Pause)
    m = evaluate_rules(rules, vars={},
                       output={"status": "Success", "stuck": False, "summary": ""})
    assert isinstance(m.action, Continue)
    # Every route into the reconciler re-arms the merge review, so its fix is reviewed.
    assert m.var_mutations == {"integration_review_needed": True}


def test_sync_worktree_follows_reconciler_and_pauses_on_failure() -> None:
    wf = load_workflow(WF)
    names = [s.name for s in _phase(wf, "integrate").steps]
    assert names.index("sync-worktree") == names.index("reconciler") + 1
    assert names.index("sync-worktree") == names.index("validate-memory") - 1
    step = next(s for s in wf.flat_steps() if s.name == "sync-worktree")
    assert step.kind == "script"
    assert step.command == "./scripts/sync-worktree.sh"
    assert step.label == "re-running worktree setup"
    assert step.show_label is False
    assert isinstance(evaluate_rules(step.rules, vars={}, output={"exit": 0}).action, Continue)
    for code in (1, 5, 124):
        m = evaluate_rules(step.rules, vars={}, output={"exit": code})
        assert isinstance(m.action, Pause), code


def test_only_finalize_routes_to_planning_and_releases_the_lane() -> None:
    wf = load_workflow(WF)
    planning = {s.name for p in wf.phases if p.name in ("spec", "plan", "implement")
                for s in p.steps}
    for step in _phase(wf, "integrate").steps:
        for rule in step.rules:
            a = rule.action
            target = getattr(a, "target", None)
            release = getattr(a, "release", None)
            if step.name == "finalize" and target == "plan-writer":
                assert release == "merge-lane", (step.name, a)
                continue
            assert target not in planning, (step.name, a)
            assert release is None, (step.name, a)


def test_integration_verify_routes_failure_to_integration_reviewer() -> None:
    wf = load_workflow(WF)
    rules = _rules(wf, "integration-verify")
    m = evaluate_rules(rules, vars={"integration_review_needed": True}, output={"exit": 0})
    assert isinstance(m.action, Continue)
    m = evaluate_rules(rules, vars={}, output={"exit": 124})
    assert isinstance(m.action, Pause)
    m = evaluate_rules(rules, vars={}, output={"exit": 1})
    assert isinstance(m.action, Goto) and m.action.target == "integration-reviewer"
    assert m.action.release is None


def test_integration_verify_skips_review_only_on_a_green_unarmed_pass() -> None:
    wf = load_workflow(WF)
    rules = _rules(wf, "integration-verify")
    skip = evaluate_rules(rules, vars={"integration_review_needed": False},
                          output={"exit": 0})
    assert skip.rule == rules[0]
    assert isinstance(skip.action, Goto) and skip.action.target == "check-cleared"
    assert skip.action.resume is False
    assert skip.var_mutations == {}
    # A failure is always triaged by the reviewer, armed or not.
    for needed in (True, False):
        m = evaluate_rules(rules, vars={"integration_review_needed": needed},
                           output={"exit": 1})
        assert isinstance(m.action, Goto) and m.action.target == "integration-reviewer"


def test_integration_review_routes_counts_to_reconciler() -> None:
    wf = load_workflow(WF)
    rules = _rules(wf, "integration-reviewer")
    base = {"status": "Success", "findings": "", "tolerated": False}
    m = evaluate_rules(rules, vars={},
                       output={**base, "critical": 1, "major": 0, "minor": 0})
    assert isinstance(m.action, Goto) and m.action.target == "reconciler"
    assert m.action.resume is True
    # major-only also reworks: the `+ output.major` term is load-bearing
    m = evaluate_rules(rules, vars={},
                       output={**base, "critical": 0, "major": 1, "minor": 0})
    assert isinstance(m.action, Goto) and m.action.target == "reconciler"
    assert m.action.resume is True
    m = evaluate_rules(rules, vars={},
                       output={**base, "critical": 0, "major": 0, "minor": 2})
    assert isinstance(m.action, Goto) and m.action.target == "reconciler"
    assert m.action.resume is True
    m = evaluate_rules(rules, vars={},
                       output={**base, "critical": 0, "major": 0, "minor": 0})
    # all zero -> clean, but the merge door re-checks the tree first
    assert isinstance(m.action, Goto) and m.action.target == "check-cleared"
    m = evaluate_rules(rules, vars={},
                       output={"status": "Error", "findings": "",
                               "critical": 0, "major": 0, "minor": 0,
                               "tolerated": False})
    assert isinstance(m.action, Pause)
    # The pause rule stays THIRD: an Error carrying findings still routes to the
    # reconciler, so a reordering that let the pause pre-empt the counts fails here.
    m = evaluate_rules(rules, vars={},
                       output={"status": "Error", "findings": "",
                               "critical": 1, "major": 0, "minor": 0,
                               "tolerated": False})
    assert isinstance(m.action, Goto) and m.action.target == "reconciler"


def test_cleanup_merge_routes_by_exit_code() -> None:
    wf = load_workflow(WF)
    step = next(s for s in wf.flat_steps() if s.name == "cleanup-merge")
    assert step.kind == "script"
    assert step.command == "./scripts/cleanup.sh"
    r = step.rules
    m0 = evaluate_rules(r, vars={}, output={"exit": 0})
    assert isinstance(m0.action, Goto) and m0.action.target == "assert-stamped"
    m2 = evaluate_rules(r, vars={}, output={"exit": 2})
    assert isinstance(m2.action, Continue)
    m1 = evaluate_rules(r, vars={}, output={"exit": 1})
    assert isinstance(m1.action, Pause)
    # exit 3 is retired: the simplified cleanup.sh never returns it, so no rule
    # matches exit 3 and it falls to the else->pause.
    m3 = evaluate_rules(r, vars={}, output={"exit": 3})
    assert isinstance(m3.action, Pause)
    resolver = next(s for s in wf.flat_steps() if s.name == "autosquash-resolve-merge")
    assert resolver.kind == "agent" and resolver.tier == "high"
    assert resolver.file is not None and resolver.file.name == "autosquash-resolve.md"
    assert resolver.rules[-1].action.target == "cleanup-merge"
    assert resolver.max_loops == 10  # rides the default; cleanup.sh owns the real 3-attempt bound
    assert resolver.clean_tree is False        # sole clean_tree opt-out


def test_autosquash_resolve_merge_is_sole_clean_tree_optout() -> None:
    wf = load_workflow(WF)
    optouts = [s.name for s in wf.flat_steps()
               if s.kind == "agent" and s.clean_tree is False]
    assert optouts == ["autosquash-resolve-merge"]


def test_assert_stamped_is_readonly_gate_before_merge() -> None:
    wf = load_workflow(WF)
    names = [s.name for s in _phase(wf, "integrate").steps]
    assert names.index("assert-stamped") == names.index("merge-to-base") - 1
    step = next(s for s in wf.flat_steps() if s.name == "assert-stamped")
    assert step.kind == "script"
    assert step.command == './scripts/assert-stamped.sh "{{ vars.slow_verify_tolerated }}"'
    m0 = evaluate_rules(step.rules, vars={}, output={"exit": 0})
    assert isinstance(m0.action, Continue)
    m1 = evaluate_rules(step.rules, vars={}, output={"exit": 1})
    assert isinstance(m1.action, Pause)


def test_merge_to_base_recovery_rules_and_cap() -> None:
    wf = load_workflow(WF)
    step = next(s for s in wf.flat_steps() if s.name == "merge-to-base")
    assert step.max_loops == 3
    rules = step.rules

    m = evaluate_rules(rules, vars={}, output={"merge": "MERGED"})
    assert isinstance(m.action, Continue)

    m = evaluate_rules(rules, vars={}, output={"merge": "DIRTY"})
    assert isinstance(m.action, Goto) and m.action.target == "reconciler"
    assert m.action.release is None
    assert m.action.feedback is not None and "uncommitted" in m.action.feedback
    assert m.rule.label == "committing leftover changes"

    m = evaluate_rules(rules, vars={}, output={"merge": "CONFLICT"})
    assert isinstance(m.action, Goto) and m.action.target == "integrate-rebase"
    assert m.action.release is None
    assert m.rule.label == "retrying integration"

    m = evaluate_rules(rules, vars={}, output={"merge": "REFUSED"})
    assert isinstance(m.action, Pause)

    m = evaluate_rules(rules, vars={}, output={"merge": "SOMETHING_ELSE"})
    assert isinstance(m.action, Pause)


def test_validate_memory_gates_post_rebase() -> None:
    wf = load_workflow(WF)
    names = [s.name for s in _phase(wf, "integrate").steps]
    assert names.index("validate-memory") == names.index("sync-worktree") + 1
    assert names.index("validate-memory") == names.index("integration-verify") - 1
    step = next(s for s in wf.flat_steps() if s.name == "validate-memory")
    assert step.kind == "script"
    assert step.command == (
        '"$IMP_PYTHON" -m imp.providers.validate_memory_cli "$IMP_PROJECT_ROOT"')
    assert step.max_loops == 6
    m0 = evaluate_rules(step.rules, vars={}, output={"exit": 0})
    assert isinstance(m0.action, Goto) and m0.action.target == "integration-verify"
    m3 = evaluate_rules(step.rules, vars={}, output={"exit": 3})
    assert isinstance(m3.action, Goto) and m3.action.target == "reconciler"
    assert m3.action.resume is True
    assert m3.action.feedback == (
        "The imp/memory/ tree is invalid against the rebased workflows. "
        "Fix the memory file paths:")
    # A crash (1) or a missing interpreter (127) is not the reconciler's to fix.
    for code in (1, 127):
        m = evaluate_rules(step.rules, vars={}, output={"exit": code})
        assert isinstance(m.action, Pause), code


def test_finalize_merge_routes_to_cleanup_merge() -> None:
    wf = load_workflow(WF)
    rules = _rules(wf, "finalize")
    m = evaluate_rules(rules, vars={"plan_index": 1, "plan_total": 1},
                       output={"decision": "merge"})
    assert isinstance(m.action, Goto) and m.action.target == "cleanup-merge"
    m = evaluate_rules(rules, vars={"plan_index": 1, "plan_total": 1},
                       output={"decision": "rework"})
    assert isinstance(m.action, Goto) and m.action.target == "plan-writer"
    assert m.action.release == "merge-lane"
    m = evaluate_rules(rules, vars={"plan_index": 1, "plan_total": 1},
                       output={"decision": "other"})
    assert isinstance(m.action, Pause)


def test_check_cleared_is_the_merge_door_before_summarizer() -> None:
    wf = load_workflow(WF)
    names = [s.name for s in _phase(wf, "integrate").steps]
    assert names.index("check-cleared") == names.index("summarizer") - 1
    step = next(s for s in wf.flat_steps() if s.name == "check-cleared")
    assert step.kind == "script"
    assert step.command == (
        './scripts/check-cleared.sh "{{ vars.slow_verify_tolerated }}"')
    assert step.show_label is False
    # The re-verify cycle is a backward goto, counted against THIS step.
    assert step.max_loops == 10
    m0 = evaluate_rules(step.rules, vars={}, output={"exit": 0})
    assert isinstance(m0.action, Continue)                    # -> summarizer
    m3 = evaluate_rules(step.rules, vars={}, output={"exit": 3})
    assert isinstance(m3.action, Goto)
    assert m3.action.target == "integration-verify"
    m1 = evaluate_rules(step.rules, vars={}, output={"exit": 1})
    assert isinstance(m1.action, Pause)


def test_unverified_tree_reruns_the_gate_with_no_human_in_the_loop() -> None:
    """The lelo51 shape: integration-verify failed, the reviewer diagnosed it as
    environmental, repaired it and returned clean+untolerated — leaving a tree no
    gate ever passed. The door sends it back and the second pass reaches the human
    gate only once the tree is cleared."""
    wf = load_workflow(WF)
    names = [s.name for s in _phase(wf, "integrate").steps]
    clean = {"status": "Success", "findings": "", "critical": 0,
             "major": 0, "minor": 0, "tolerated": False}
    m = evaluate_rules(_rules(wf, "integration-reviewer"), vars={}, output=clean)
    assert isinstance(m.action, Goto) and m.action.target == "check-cleared"
    back = evaluate_rules(_rules(wf, "check-cleared"), vars={}, output={"exit": 3})
    assert isinstance(back.action, Goto)
    assert back.action.target == "integration-verify"
    passed = evaluate_rules(_rules(wf, "integration-verify"),
                            vars={"integration_review_needed": True}, output={"exit": 0})
    assert isinstance(passed.action, Continue)
    assert names[names.index("integration-verify") + 1] == "integration-reviewer"
    ok = evaluate_rules(_rules(wf, "check-cleared"), vars={}, output={"exit": 0})
    assert isinstance(ok.action, Continue)
    assert names[names.index("check-cleared") + 1] == "summarizer"

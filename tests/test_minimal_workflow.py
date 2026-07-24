from __future__ import annotations

from imp.agents.prompts import build_context, render_prompt
from imp.assets import bundled_workflow_dir
from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.state import InMemoryEventLog
from imp.engine.types import Continue, Goto

MINIMAL = bundled_workflow_dir("minimal")


def _wf():
    return load_workflow(MINIMAL)


def _steps(wf):
    return {s.name: s for s in wf.flat_steps()}


def test_minimal_loads_with_the_four_named_steps() -> None:
    wf = _wf()
    names = [s.name for s in wf.flat_steps()]
    assert names == ["front-door", "implement", "review", "finalize"]
    # No vestigial name: field — identity follows the directory.
    assert wf.name == "minimal"


def test_minimal_declares_no_workflow_vars() -> None:
    # base_head is removed; the minimal workflow declares no vars.
    assert _wf().vars.types() == {}


def test_minimal_has_no_capture_base_step_or_script() -> None:
    names = [s.name for s in _wf().flat_steps()]
    assert "capture-base" not in names
    assert not (MINIMAL / "scripts" / "capture-base-head.sh").exists()


def test_front_door_is_interactive_high_tier_status_only() -> None:
    wf = _wf()
    fd = _steps(wf)["front-door"]
    assert fd.interactive is True
    assert fd.tier == "high"
    # status enum only — no ceremony (that is a default-workflow concept).
    assert set(wf.agents["front-door"].output_schema.field_names()) == {"status"}


def test_front_door_allows_worktree_and_keeps_naming_parking_and_scouting() -> None:
    tools = _wf().agents["front-door"].tools
    assert "create_and_enter_worktree" in tools   # forks the worktree on go-live
    assert "save_and_exit" in tools               # parking stays available
    assert "rename_quest" in tools                # it names the quest
    assert "sub_agent" in tools                   # explorer/researcher/spiker


def test_front_door_body_includes_partials_and_goes_live_in_worktree() -> None:
    body = _wf().agents["front-door"].prompt_template_source
    assert '{% include "partials/front-door.md" %}' in body
    assert '{% include "partials/go-live-worktree.md" %}' in body
    assert "{{ quest.home }}/artifacts/spec/{{ quest.id }}.md" in body
    # The stale in-place framing is gone.
    assert "in place on the current branch" not in body


def test_front_door_renders_full_include_closure() -> None:
    # Rendering the front-door agent resolves the whole shared closure with no
    # leftover {% include %}. workflow_dir=MINIMAL has no local partials/, so the
    # render-loader fallback resolves them from the bundled shared pool.
    body = _wf().agents["front-door"].prompt_template_source
    out = render_prompt(
        body,
        build_context(quest_title="t", event_log=InMemoryEventLog(),
                      quest_id="abc", quest_home=".imp/quests/abc",
                      project_facts={}),
        workflow_dir=MINIMAL)
    assert "{% include" not in out                      # every include resolved
    assert "Measure twice, cut once" in out             # action-care leaf
    assert "explorer" in out.lower()                    # delegate-exploration leaf
    assert "create_and_enter_worktree" in out           # go-live worktree include


def test_implement_is_high_tier_writer_that_commits() -> None:
    wf = _wf()
    impl = _steps(wf)["implement"]
    assert impl.tier == "high"
    assert impl.interactive is False
    assert set(wf.agents["implementer"].output_schema.field_names()) == {"status"}
    tools = wf.agents["implementer"].tools
    assert {"edit_file", "write_file", "bash"} <= tools
    for banned in ("create_and_enter_worktree", "save_and_exit", "rename_quest"):
        assert banned not in tools


def test_review_is_read_only_high_tier_and_diffs_fork_point() -> None:
    wf = _wf()
    rev = _steps(wf)["review"]
    assert rev.tier == "high"
    assert rev.read_only == (".",)
    schema = set(wf.agents["reviewer"].output_schema.field_names())
    assert schema == {"status", "verdict", "findings"}
    verdict = next(f for f in wf.agents["reviewer"].output_schema.fields
                   if f.name == "verdict")
    assert set(verdict.values) == {"approve", "rework"}
    body = wf.agents["reviewer"].prompt_template_source
    assert "{{ quest.cur_base_head }}" in body               # the fork-point diff base
    assert "git diff {{ quest.cur_base_head }} HEAD" in body
    assert "{{ vars.base_head }}" not in body                # the removed var is gone


def test_review_routes_rework_to_implement_and_approve_completes() -> None:
    rules = _steps(_wf())["review"].rules
    rework = evaluate_rules(
        rules, vars={},
        output={"status": "Success", "verdict": "rework", "findings": "fix x"})
    assert isinstance(rework.action, Goto)
    assert rework.action.target == "implement"
    assert rework.action.resume is True
    assert rework.action.feedback == "Here's review feedback to address:"
    approve = evaluate_rules(
        rules, vars={},
        output={"status": "Success", "verdict": "approve", "findings": ""})
    assert isinstance(approve.action, Continue)          # off the end -> _complete


def test_finalize_is_interactive_high_tier_status_only_and_can_write() -> None:
    wf = _wf()
    fin = _steps(wf)["finalize"]
    assert fin.interactive is True
    assert fin.tier == "high"
    # Not read-only — contrast the reviewer, whose read_only == (".",).
    assert fin.read_only == ()
    agent = wf.agents["finalize"]
    # Drives the interactive gate with impty.
    assert agent.drivers == {"impty"}
    # Status only — no merge/rework/pause routing.
    assert set(agent.output_schema.field_names()) == {"status"}
    tools = agent.tools
    # It applies and commits requested tweaks itself.
    assert {"edit_file", "write_file", "bash"} <= tools
    # deny_tools mirrors the other minimal coding agents.
    for banned in ("rename_quest", "create_and_enter_worktree", "save_and_exit"):
        assert banned not in tools


def test_finalize_body_guards_on_autonomous_with_both_branches() -> None:
    body = _wf().agents["finalize"].prompt_template_source

    def _render(autonomous: bool) -> str:
        return render_prompt(
            body,
            build_context(quest_title="t", event_log=InMemoryEventLog(),
                          quest_id="abc", quest_home=".imp/quests/abc",
                          project_facts={}, autonomous=autonomous),
            workflow_dir=MINIMAL)

    attended = _render(False)
    autonomous = _render(True)
    # Every include resolves in both branches.
    assert "{% include" not in attended
    assert "{% include" not in autonomous
    # Attended branch opens the human gate — the live-gate-dialogue partial resolved.
    assert "Converse until ready" in attended
    # Autonomous branch auto-completes without a gate.
    assert "complete immediately without soliciting" in autonomous
    # The guard genuinely branches: the human-gate leaf is absent under autonomy.
    assert "Converse until ready" not in autonomous
    assert "complete immediately without soliciting" not in attended


def test_minimal_front_door_forks_worktree_others_do_not() -> None:
    wf = _wf()
    assert all(s.kind != "merge_and_exit_worktree" for s in wf.flat_steps())
    assert "create_and_enter_worktree" in wf.agents["front-door"].tools
    for role in ("implementer", "reviewer", "finalize"):
        assert "create_and_enter_worktree" not in wf.agents[role].tools


def test_implement_commits_in_the_worktree() -> None:
    body = _wf().agents["implementer"].prompt_template_source
    # Commits happen in the quest's worktree, not "in place on the current branch".
    assert "worktree" in body
    assert "directly on the current branch" not in body
    assert "no worktree" not in body

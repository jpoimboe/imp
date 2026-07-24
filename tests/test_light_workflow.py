from __future__ import annotations

import os
import subprocess
from pathlib import Path

from imp.agents.prompts import build_context, render_prompt
from imp.assets import bundled_workflow_dir
from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.state import InMemoryEventLog
from imp.engine.types import Continue, Goto

LIGHT = bundled_workflow_dir("light")


def _wf():
    return load_workflow(LIGHT)


def _steps(wf):
    return {s.name: s for s in wf.flat_steps()}


def test_light_loads_with_the_three_named_steps_plus_capture() -> None:
    wf = _wf()
    names = [s.name for s in wf.flat_steps()]
    assert names == ["front-door", "capture-base", "implement", "review"]
    # No vestigial name: field — identity follows the directory.
    assert wf.name == "light"


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
    # leftover {% include %}. workflow_dir=LIGHT has no local partials/, so the
    # render-loader fallback resolves them from the bundled shared pool.
    body = _wf().agents["front-door"].prompt_template_source
    out = render_prompt(
        body,
        build_context(quest_title="t", event_log=InMemoryEventLog(),
                      quest_id="abc", quest_home=".imp/quests/abc",
                      project_facts={}),
        workflow_dir=LIGHT)
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
        rules, vars={"base_head": ""},
        output={"status": "Success", "verdict": "rework", "findings": "fix x"})
    assert isinstance(rework.action, Goto)
    assert rework.action.target == "implement"
    assert rework.action.resume is True
    assert rework.action.feedback == "Here's review feedback to address:"
    approve = evaluate_rules(
        rules, vars={"base_head": ""},
        output={"status": "Success", "verdict": "approve", "findings": ""})
    assert isinstance(approve.action, Continue)          # off the end -> _complete


def test_light_declares_base_head_var() -> None:
    types = _wf().vars.types()
    assert "base_head" in types


def test_capture_base_step_sets_base_head_from_stdout() -> None:
    cap = _steps(_wf())["capture-base"]
    assert cap.command == "./scripts/capture-base-head.sh"
    match = evaluate_rules(
        cap.rules, vars={"base_head": ""},
        output={"exit": 0, "stdout": "deadbeefcafe", "stderr": ""})
    assert match.var_mutations == {"base_head": "deadbeefcafe"}
    assert isinstance(match.action, Continue)


def test_light_front_door_forks_worktree_others_do_not() -> None:
    wf = _wf()
    assert all(s.kind != "merge_and_exit_worktree" for s in wf.flat_steps())
    assert "create_and_enter_worktree" in wf.agents["front-door"].tools
    for role in ("implementer", "reviewer"):
        assert "create_and_enter_worktree" not in wf.agents[role].tools


def test_capture_base_head_script_echoes_head(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    def run(*a):
        return subprocess.run(a, cwd=repo, check=True,
                              capture_output=True, text=True)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    (repo / "f.txt").write_text("hi\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "seed")
    expected = run("git", "rev-parse", "HEAD").stdout.strip()
    script = LIGHT / "scripts" / "capture-base-head.sh"
    out = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        env={**os.environ, "IMP_PROJECT_ROOT": str(repo)},
        check=True, capture_output=True, text=True)
    assert out.stdout.strip() == expected

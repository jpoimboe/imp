from __future__ import annotations

from imp.agents.prompts import build_context, render_prompt
from imp.assets import bundled_workflow_dir
from imp.engine.loader import load_workflow
from imp.engine.state import JsonlEventLog


def _wf():
    return load_workflow(bundled_workflow_dir("default"))


def test_explorer_agent_is_read_only_leaf() -> None:
    a = _wf().agents["explorer"]
    assert a.name == "explorer"
    tools = set(a.tools)
    assert tools == {"read_file", "glob", "grep", "bash", "symbol_outline"}
    for forbidden in ("write_file", "edit_file", "web_search", "web_fetch",
                      "sub_agent", "wait_for_user"):
        assert forbidden not in tools
    assert a.subagents == ()
    # A dispatched subagent carries no output schema — it reports in prose.
    assert a.output_schema.fields == ()


def test_researcher_agent_has_web_tools_read_only() -> None:
    a = _wf().agents["researcher"]
    assert set(a.tools) == {"read_file", "glob", "grep", "bash",
                             "web_search", "web_fetch"}
    assert a.subagents == ()
    assert a.output_schema.fields == ()


def test_subagents_have_pinned_static_tier() -> None:
    wf = _wf()
    assert wf.agents["explorer"].tier == "medium"
    assert wf.agents["researcher"].tier == "high"
    assert wf.agents["spiker"].tier == "high"


def test_plan_writer_declares_research_subagents() -> None:
    a = _wf().agents["plan-writer"]
    assert "sub_agent" in a.tools
    assert set(a.subagents) == {"explorer", "researcher", "spiker"}


def test_spec_writer_declares_research_subagents() -> None:
    a = _wf().agents["spec-finalizer"]
    assert "sub_agent" in a.tools
    assert set(a.subagents) == {"explorer", "researcher", "spiker"}


def test_spec_writer_prose_relaxes_throttle_and_adds_research(tmp_path) -> None:
    body = _wf().agents["threshold"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    out = render_prompt(body, build_context(
        quest_title="t", event_log=log, quest_id="abc",
        quest_home=".imp/quests/abc"),
        workflow_dir=bundled_workflow_dir("default"))
    assert "no more than 3 question rounds" not in out
    assert "Do not over-research a clear request" not in out
    assert "Research before you converge" in out
    assert 'sub_agent(subagent_name="explorer"' in out
    assert "researcher" in out
    assert "no cost gating" in out


def test_delegate_partial_included_in_explore_heavy_steps() -> None:
    wf = _wf()
    for name in ("plan-writer", "spec-finalizer", "task-lead", "finalize"):
        body = wf.agents[name].prompt_template_source
        assert '{% include "partials/delegate-exploration.md" %}' in body, name


def test_task_lead_declares_research_subagents() -> None:
    a = _wf().agents["task-lead"]
    assert set(a.subagents) == {"coder", "code-reviewer", "explorer", "researcher", "spiker"}
    assert "sub_agent" in a.tools


def test_finalize_declares_research_subagents() -> None:
    a = _wf().agents["finalize"]
    assert set(a.subagents) == {"explorer", "researcher", "spiker"}
    assert "sub_agent" in a.tools


def test_reviewers_declare_explorer_and_include_the_verification_partial() -> None:
    wf = _wf()
    include = '{% include "partials/delegate-verification.md" %}'
    for name in ("spec-reviewer", "plan-reviewer"):
        a = wf.agents[name]
        assert a.subagents == ("explorer",), name
        assert "sub_agent" in a.tools, name
        body = a.prompt_template_source
        assert include in body, name
        # The inline-only mandate the partial supersedes is gone, not softened.
        assert "with `read_file`/`grep`" not in body, name
    spec_body = wf.agents["spec-reviewer"].prompt_template_source
    assert spec_body.index("## Your Role") < spec_body.index(include)
    assert spec_body.index(include) < spec_body.index("## Process")
    plan_body = wf.agents["plan-reviewer"].prompt_template_source
    assert plan_body.index("Do not read prior or future") < plan_body.index(include)
    assert plan_body.index(include) < plan_body.index(
        "## Review the prescribed code like a diff (primary)")


def test_delegate_exploration_partial_exists_and_carries_the_rule() -> None:
    from pathlib import Path
    p = Path("src/imp/assets/partials/delegate-exploration.md")
    text = p.read_text()
    # The default rule: delegate to explorer/researcher; read inline only what you edit.
    assert "read inline only what you will edit" in text.lower()
    assert 'sub_agent(subagent_name="explorer"' in text
    assert "researcher" in text


def test_delegate_verification_partial_exists_and_carries_the_rule() -> None:
    from pathlib import Path
    text = Path("src/imp/assets/partials/delegate-verification.md").read_text()
    # The reviewer-shaped rule: retrieval fans out, the verdict stays with the reader.
    assert "Delegate retrieval, never judgment" in text
    assert 'sub_agent(subagent_name="explorer"' in text
    assert "a subagent's opinion is not a finding" in text
    # Reviewers get explorer only: no web-facing researcher, and no evidence they
    # would then be judging themselves.
    assert "researcher" not in text
    assert "spiker" not in text


_PLAN_WRITER_SPIKER_PARAGRAPH = (
    "Dispatch a **`spiker`** when a plan-time unknown can only be settled by running "
    "code — what an existing test actually does under the change, which call sites a "
    "refactor really touches, whether a seam behaves as the spec assumes. Reach for "
    "one before writing a **derived list** into the plan — which tests, which call "
    "sites, which of N get treatment X — because a list derived by hand is re-derived "
    "by hand at every review lap. These spikes settle **how** to build what the spec "
    "already fixed; an outcome that would change **what** the quest delivers belongs "
    "back in the spec, not here. It works in a disposable worktree and parks its "
    "script and output under `{{ quest.home }}/spike/`; cite that evidence in the "
    "plan so the reviewer checks it instead of re-deriving it."
)

_PLAN_WRITER_SPIKE_LINE = (
    "Read `{{ quest.home }}/spike/` if it exists — empirical evidence a spiker "
    "captured. A recorded result is settled fact: build on it, don't re-derive it."
)

_DELEGATE_INCLUDE = '{% include "partials/delegate-exploration.md" %}'


def test_spiker_guidance_stays_per_agent_not_in_the_shared_partial() -> None:
    from pathlib import Path
    shared = Path("src/imp/assets/partials/delegate-exploration.md").read_text()
    assert "spiker" not in shared
    # When to reach for one differs by phase, so each includer carries its own cue.
    body = _wf().agents["plan-writer"].prompt_template_source
    assert _PLAN_WRITER_SPIKER_PARAGRAPH in body
    assert body.index(_DELEGATE_INCLUDE) < body.index(_PLAN_WRITER_SPIKER_PARAGRAPH)


def test_plan_writer_reads_the_spike_record_before_delegating() -> None:
    body = _wf().agents["plan-writer"].prompt_template_source
    assert _PLAN_WRITER_SPIKE_LINE in body
    assert body.index("Read relevant source files") < body.index(_PLAN_WRITER_SPIKE_LINE)
    assert body.index(_PLAN_WRITER_SPIKE_LINE) < body.index(_DELEGATE_INCLUDE)


def test_brainstorm_dialogue_includes_partial_and_drops_inline_duplication() -> None:
    from pathlib import Path
    body = Path(
        "src/imp/assets/partials/brainstorm-dialogue.md"
    ).read_text()
    assert '{% include "partials/delegate-exploration.md" %}' in body
    # The extracted delegation specifics no longer live inline in brainstorm-dialogue.
    assert "prefer dispatching\nparallel" not in body
    assert "fan deep mapping out to the subagents" not in body


def test_spec_writer_design_quality_and_coverage_guidance(tmp_path) -> None:
    body = _wf().agents["threshold"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    out = render_prompt(body, build_context(
        quest_title="t", event_log=log, quest_id="abc",
        quest_home=".imp/quests/abc"),
        workflow_dir=bundled_workflow_dir("default"))
    assert "present it in sections" in out
    assert "do not propose unrelated refactoring" not in out
    assert "improvements to the code this quest touches." in out
    assert "architecture, components, data flow, error handling, and testing" in out


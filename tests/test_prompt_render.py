"""Tests for render_prompt / build_context.

Seven namespaces: quest, events, vars, run, step, docs, project
(project.verify_gate_unset — the fast test gate is unwired).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.prompts import PromptRenderError, build_context, render_prompt
from imp.assets import bundled_docs_dir, bundled_workflow_dir
from imp.engine.state import EventLog, JsonlEventLog


def _log(path: Path, events: list[tuple[str, dict]]) -> EventLog:
    log = JsonlEventLog(path)
    for t, p in events:
        log.append(agent_id=None, type=t, payload=p)
    return log


def test_change_namespace_no_longer_binds(tmp_path: Path) -> None:
    """The old top-level `change` name is unbound; only `quest` resolves."""
    ctx = build_context(
        quest_title="t", event_log=_log(tmp_path / "e.jsonl", []), quest_id="x",
    )
    # `change` is no longer a passed top-level name: under StrictUndefined,
    # rendering it at all raises (surfaced as PromptRenderError).
    with pytest.raises(PromptRenderError):
        render_prompt("{{ change }}", ctx)
    assert render_prompt("{{ quest.id }}", ctx) == "x"


def test_renders_quest_namespace(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(
        quest_title="My Quest", event_log=log, quest_id="myid01",
    )
    assert render_prompt(
        "Hello {{ quest.title }} ({{ quest.id }})", ctx,
    ) == "Hello My Quest (myid01)"


def test_renders_events_namespace(tmp_path: Path) -> None:
    log = _log(
        tmp_path / "events.jsonl",
        [
            ("step_started", {"step": "spec-writer"}),
            ("agent_result", {"status": "Success", "summary": "hi"}),
            ("step_completed", {"step": "spec-writer", "action": "continue"}),
        ],
    )
    ctx = build_context(
        quest_title="t",
        event_log=log,
    )
    assert render_prompt(
        "Prev: {{ events['spec-writer'].summary }}", ctx,
    ) == "Prev: hi"


def test_events_missing_step_renders_empty(tmp_path: Path) -> None:
    ctx = build_context(
        quest_title="t",
        event_log=_log(tmp_path / "events.jsonl", []),
    )
    assert render_prompt("{{ events['nope'].field }}", ctx) == ""


def test_undefined_top_level_raises(tmp_path: Path) -> None:
    ctx = build_context(
        quest_title="t",
        event_log=_log(tmp_path / "events.jsonl", []),
    )
    with pytest.raises(PromptRenderError):
        render_prompt("{{ bogus.thing }}", ctx)


def test_renders_vars_namespace(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(quest_title="t", event_log=log,
                        vars={"fast_path": True})
    assert render_prompt("{{ vars.fast_path }}", ctx) == "True"
    assert render_prompt("{% if vars.fast_path %}fast{% endif %}", ctx) == "fast"


def test_vars_typo_raises_strict_within(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(quest_title="t", event_log=log,
                        vars={"fast_path": False})
    with pytest.raises(PromptRenderError):
        render_prompt("{{ vars.fastpath }}", ctx)


def test_vars_default_empty_when_omitted(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(quest_title="t", event_log=log)
    # An unknown var still raises (strict-within over an empty map).
    with pytest.raises(PromptRenderError):
        render_prompt("{{ vars.anything }}", ctx)


def test_coder_prompt_gates_tdd_on_fast_path(tmp_path: Path) -> None:
    """The shipped coder template must render two branches: fast_path
    waives tests; else loads TDD. StrictUndefined means both renders pass
    fast_path explicitly (there is no undefined->else fallback)."""
    from imp.assets import bundled_workflow_dir
    from imp.engine.loader import load_workflow

    wf = load_workflow(bundled_workflow_dir("default"))
    body = wf.agents["coder"].prompt_template_source
    log = _log(tmp_path / "events.jsonl", [])

    fast = render_prompt(
        body,
        build_context(quest_title="t", event_log=log,
                      vars={"fast_path": True}),
        workflow_dir=bundled_workflow_dir("default"),
    )
    slow = render_prompt(
        body,
        build_context(quest_title="t", event_log=log,
                      vars={"fast_path": False}),
        workflow_dir=bundled_workflow_dir("default"),
    )
    # fast-path: no new tests, TDD skill NOT loaded; the coder does NOT run
    # the suite or a lint gate itself — the phase-level verify gate owns that.
    assert "do **not** create or modify tests" in fast
    assert "IMP_LINT_COMMAND" not in fast
    assert "phase-level" in fast
    assert "test-driven-development" not in fast
    # standard path: TDD skill loaded; fast-path constraints absent.
    assert "test-driven-development" in slow
    assert "do **not** create or modify tests" not in slow


def test_events_render_skips_torn_trailing_line(tmp_path: Path) -> None:
    """AC7: torn trailing events-file line is skipped; prior good event still renders."""
    path = tmp_path / "events.jsonl"
    log = _log(
        path,
        [
            ("step_started", {"step": "spec-writer"}),
            ("agent_result", {"status": "Success", "summary": "good"}),
        ],
    )
    # Simulate a crash mid-append: a partial JSON line, no trailing newline.
    with path.open("a") as f:
        f.write('{"type": "agent_result", "payload": {"summa')
    ctx = build_context(quest_title="t", event_log=log)
    assert render_prompt(
        "Prev: {{ events['spec-writer'].summary }}", ctx,
    ) == "Prev: good"


def test_renders_project_verify_gate_unset(tmp_path: Path) -> None:
    """The gate boolean reaches templates and drives a conditional block."""
    log = _log(tmp_path / "events.jsonl", [])
    tmpl = "{% if project.verify_gate_unset %}WIRE IT{% endif %}|{{ project.verify_gate_unset }}"
    unset = build_context(quest_title="t", event_log=log, verify_gate_unset=True)
    wired = build_context(quest_title="t", event_log=log)
    assert render_prompt(tmpl, unset) == "WIRE IT|True"
    assert render_prompt(tmpl, wired) == "|False"


def test_unknown_project_attribute_raises(tmp_path: Path) -> None:
    """A typo'd attribute on the real project namespace must still raise under StrictUndefined."""
    ctx = build_context(
        quest_title="t",
        event_log=_log(tmp_path / "events.jsonl", []),
    )
    with pytest.raises(PromptRenderError) as ei:
        render_prompt("{{ project.x }}", ctx)
    assert "fact" not in str(ei.value)   # no facts-specific message survives


def test_renders_quest_home(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(
        quest_id="abc123", quest_title="My Quest",
        quest_home="/ctl/.imp/quests/abc123", event_log=log,
    )
    assert render_prompt(
        "spec at {{ quest.home }}/artifacts/spec/{{ quest.id }}.md", ctx,
    ) == "spec at /ctl/.imp/quests/abc123/artifacts/spec/abc123.md"


def test_quest_home_defaults_empty(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(quest_id="t", quest_title="t", event_log=log)
    assert render_prompt("{{ quest.home }}", ctx) == ""



def test_missing_run_attr_raises(tmp_path: Path) -> None:
    ctx = build_context(
        quest_title="t", event_log=_log(tmp_path / "events.jsonl", []),
    )
    with pytest.raises(PromptRenderError):
        render_prompt("{{ run.bogus }}", ctx)


def test_renders_cur_base_head(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(quest_title="t", event_log=log, cur_base_head="deadbeef")
    assert render_prompt("{{ quest.cur_base_head }}", ctx) == "deadbeef"


def test_cur_base_head_defaults_empty(tmp_path: Path) -> None:
    ctx = build_context(
        quest_title="t", event_log=_log(tmp_path / "events.jsonl", []),
    )
    assert render_prompt("{{ quest.cur_base_head }}", ctx) == ""


def test_render_resolves_include_with_workflow_dir(tmp_path: Path) -> None:
    (tmp_path / "partials").mkdir()
    (tmp_path / "partials" / "frag.md").write_text("SHARED-FRAGMENT\n")
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(quest_title="t", event_log=log)
    out = render_prompt(
        '{% include "partials/frag.md" %}', ctx, workflow_dir=tmp_path)
    assert "SHARED-FRAGMENT" in out


def test_render_without_workflow_dir_cannot_resolve_include(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(quest_title="t", event_log=log)
    with pytest.raises(PromptRenderError):
        render_prompt('{% include "partials/frag.md" %}', ctx)



def test_plain_gate_asks_build_affirmative(tmp_path: Path) -> None:
    src = Path(
        "src/imp/assets/partials/brainstorm-dialogue.md"
    ).read_text()
    log = _log(tmp_path / "events.jsonl", [])
    rendered = render_prompt(
        src,
        build_context(quest_title="t", event_log=log),
        workflow_dir=bundled_workflow_dir("default"))
    assert "a single recommendation-anchored question, not a menu" in rendered
    assert "save this to your backlog" not in rendered


def test_renders_run_autonomous_true(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(quest_title="t", event_log=log, autonomous=True)
    assert render_prompt("{{ run.autonomous }}", ctx) == "True"
    assert render_prompt(
        "{% if run.autonomous %}take{% else %}confirm{% endif %}", ctx) == "take"


def test_renders_run_autonomous_false_default(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(quest_title="t", event_log=log)
    assert render_prompt("{{ run.autonomous }}", ctx) == "False"
    assert render_prompt(
        "{% if run.autonomous %}take{% else %}confirm{% endif %}", ctx) == "confirm"


def test_quest_namespace_exposes_orig_base_head(tmp_path) -> None:
    from imp.agents.prompts import build_context, render_prompt
    from imp.engine.state import JsonlEventLog

    log = JsonlEventLog(tmp_path / "events.jsonl")
    ctx = build_context(
        quest_title="t", event_log=log, quest_id="abc",
        cur_base_head="NEWTIP", orig_base_head="OLDTIP")
    # Both heads render, so a template can branch on whether the base moved.
    out = render_prompt(
        "{{ quest.orig_base_head }}|{{ quest.cur_base_head }}|"
        "{% if quest.cur_base_head != quest.orig_base_head %}MOVED{% else %}SAME{% endif %}",
        ctx)
    assert out == "OLDTIP|NEWTIP|MOVED"

    same = build_context(
        quest_title="t", event_log=log, quest_id="abc",
        cur_base_head="X", orig_base_head="X")
    out2 = render_prompt(
        "{% if quest.cur_base_head != quest.orig_base_head %}MOVED{% else %}SAME{% endif %}",
        same)
    assert out2 == "SAME"


def test_build_or_save_attended_build_default_yields(tmp_path: Path) -> None:
    src = Path(
        "src/imp/assets/partials/brainstorm-dialogue.md"
    ).read_text()
    log = _log(tmp_path / "events.jsonl", [])
    rendered = render_prompt(
        src, build_context(quest_title="t", event_log=log),
        workflow_dir=bundled_workflow_dir("default"))
    assert "a single recommendation-anchored question, not a menu" in rendered
    assert "end your turn to yield" in rendered
    assert "take the default and proceed without yielding" not in rendered
    assert "headless run" not in src
    assert "--non-interactive" not in rendered
    assert "run.non_interactive" not in src


def test_renders_run_opening_when_present(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(quest_title="t", event_log=log,
                        opening="add a dark-mode toggle")
    assert ctx.run.opening == "add a dark-mode toggle"
    assert render_prompt(
        "{% if run.opening %}SEED:{{ run.opening }}{% else %}BARE{% endif %}",
        ctx) == "SEED:add a dark-mode toggle"


def test_run_opening_defaults_empty_and_selects_bare_branch(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(quest_title="t", event_log=log)
    assert ctx.run.opening == ""
    assert render_prompt(
        "{% if run.opening %}SEED{% else %}BARE{% endif %}", ctx) == "BARE"


def test_step_namespace_defaults_to_lap_one(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(quest_title="t", event_log=log)
    assert render_prompt("{{ step.lap }}", ctx) == "1"
    assert render_prompt("{{ step.prior_laps | length }}", ctx) == "0"


def test_step_namespace_lap_is_last_recorded_plus_one(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(
        quest_title="t", event_log=log,
        prior_laps=[
            {"lap": 2, "outcome": "first draft", "head": "cafe1234"},
            {"lap": 3, "outcome": "reworked", "head": "beef5678"},
        ],
    )
    # lap is last-recorded + 1 (3 + 1 = 4), NOT len + 1 (which would be 3).
    assert render_prompt("{{ step.lap }}", ctx) == "4"
    # prior_laps carries the recorded entries; templates can read the last head.
    assert render_prompt("{{ step.prior_laps[-1].head }}", ctx) == "beef5678"
    assert render_prompt("{{ step.prior_laps[-1].outcome }}", ctx) == "reworked"
    assert render_prompt("{{ step.prior_laps | length }}", ctx) == "2"


def test_step_namespace_lap_tracks_recorded_lap_not_list_length(tmp_path: Path) -> None:
    # The recorded history is bounded to the last OUTCOME_HISTORY_K (=4) laps, so
    # after many re-entries the list length saturates while the stored `lap` keeps
    # climbing. step.lap must follow the recorded lap, not len(prior_laps): here a
    # 4-entry truncated history whose last recorded lap is 6 -> this render is lap 7
    # (len-based derivation would wrongly yield 5).
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(
        quest_title="t", event_log=log,
        prior_laps=[
            {"lap": 3, "outcome": "l3", "head": "aaa3"},
            {"lap": 4, "outcome": "l4", "head": "aaa4"},
            {"lap": 5, "outcome": "l5", "head": "aaa5"},
            {"lap": 6, "outcome": "l6", "head": "aaa6"},
        ],
    )
    assert render_prompt("{{ step.lap }}", ctx) == "7"
    assert render_prompt("{{ step.prior_laps | length }}", ctx) == "4"


def test_build_or_save_autonomous_takes_build_default(tmp_path: Path) -> None:
    src = Path(
        "src/imp/assets/partials/brainstorm-dialogue.md"
    ).read_text()
    log = _log(tmp_path / "events.jsonl", [])
    rendered = render_prompt(
        src, build_context(quest_title="t", event_log=log, autonomous=True),
        workflow_dir=bundled_workflow_dir("default"))
    assert "take the default and proceed without yielding" in rendered
    assert "Want me to build this out now?" not in rendered
    assert "create_and_enter_worktree" in rendered


def test_docs_namespace_defaults_to_resolved_corpus(tmp_path: Path) -> None:
    # With no explicit docs_path, build_context resolves the real corpus path,
    # so every agent prompt gets the right location with no per-call wiring.
    ctx = build_context(quest_title="t", event_log=_log(tmp_path / "e.jsonl", []))
    assert render_prompt("{{ docs.path }}", ctx) == str(bundled_docs_dir())


def test_docs_namespace_accepts_explicit_override(tmp_path: Path) -> None:
    ctx = build_context(
        quest_title="t", event_log=_log(tmp_path / "e.jsonl", []),
        docs_path="/abs/imp/docs",
    )
    assert render_prompt("corpus at {{ docs.path }}", ctx) == "corpus at /abs/imp/docs"


def test_docs_namespace_is_strict_on_unknown_attr(tmp_path: Path) -> None:
    ctx = build_context(
        quest_title="t", event_log=_log(tmp_path / "e.jsonl", []),
        docs_path="/x",
    )
    with pytest.raises(PromptRenderError):
        render_prompt("{{ docs.typo }}", ctx)


def test_quest_parent_and_depends_on_available_to_templates(tmp_path: Path) -> None:
    ctx = build_context(
        quest_title="t", event_log=_log(tmp_path / "e.jsonl", []),
        parent={"id": "par001", "title": "Parent Quest"},
        depends_on=[{"id": "dep001", "title": "Dep One", "merged": False},
                    {"id": "dep002", "title": "Dep Two", "merged": True}],
    )
    src = (
        "{% if quest.parent %}P:{{ quest.parent.id }}/{{ quest.parent.title }};{% endif %}"
        "{% for d in quest.depends_on %}[{{ d.id }}:{{ 'M' if d.merged else 'U' }}]{% endfor %}"
        "{% if quest.depends_on | rejectattr('merged') | list %}BLOCKED{% endif %}"
    )
    assert render_prompt(src, ctx) == "P:par001/Parent Quest;[dep001:U][dep002:M]BLOCKED"


def test_quest_parent_and_depends_on_empty_by_default(tmp_path: Path) -> None:
    ctx = build_context(quest_title="t", event_log=_log(tmp_path / "e.jsonl", []))
    src = ("{% if quest.parent %}HASPARENT{% endif %}"
           "{% if quest.depends_on %}HASDEPS{% endif %}")
    assert render_prompt(src, ctx) == ""


def test_quest_all_merged_deps_are_not_blocked(tmp_path: Path) -> None:
    ctx = build_context(
        quest_title="t", event_log=_log(tmp_path / "e.jsonl", []),
        depends_on=[{"id": "dep001", "title": "Dep One", "merged": True}],
    )
    src = "{% if quest.depends_on | rejectattr('merged') | list %}BLOCKED{% else %}CLEAR{% endif %}"
    assert render_prompt(src, ctx) == "CLEAR"


def test_renders_base_and_checkout_branch(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(quest_title="t", event_log=log,
                        base_branch="release-2.0", checkout_branch="main")
    # Both fields render independently, so a template can branch on divergence.
    assert render_prompt(
        "{{ quest.base_branch }}|{{ quest.checkout_branch }}|"
        "{% if quest.checkout_branch and quest.checkout_branch != quest.base_branch %}"
        "DIVERGED{% else %}SAME{% endif %}", ctx) == "release-2.0|main|DIVERGED"


def test_base_and_checkout_branch_default_empty(tmp_path: Path) -> None:
    ctx = build_context(
        quest_title="t", event_log=_log(tmp_path / "events.jsonl", []))
    # An unpopulated checkout_branch never reads as divergence.
    assert render_prompt(
        "[{{ quest.base_branch }}][{{ quest.checkout_branch }}]"
        "{% if quest.checkout_branch and quest.checkout_branch != quest.base_branch %}"
        "DIVERGED{% else %}SAME{% endif %}", ctx) == "[][]SAME"


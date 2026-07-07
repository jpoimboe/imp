"""Tests for render_prompt / build_context (5 namespaces: change, events, vars, project, run)."""

from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.prompts import PromptRenderError, build_context, render_prompt
from imp.assets import bundled_workflow_dir
from imp.engine.state import EventLog, JsonlEventLog


def _log(path: Path, events: list[tuple[str, dict]]) -> EventLog:
    log = JsonlEventLog(path)
    for t, p in events:
        log.append(agent_id=None, type=t, payload=p)
    return log


def test_renders_change_namespace(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(
        change_title="My Change", event_log=log, change_id="myid01",
    )
    assert render_prompt(
        "Hello {{ change.title }} ({{ change.id }})", ctx,
    ) == "Hello My Change (myid01)"


def test_renders_events_namespace(tmp_path: Path) -> None:
    log = _log(
        tmp_path / "events.jsonl",
        [
            ("step_started", {"step": "spec-writer"}),
            ("agent_output", {"status": "Success", "summary": "hi"}),
            ("step_completed", {"step": "spec-writer", "action": "continue"}),
        ],
    )
    ctx = build_context(
        change_title="t",
        event_log=log,
    )
    assert render_prompt(
        "Prev: {{ events['spec-writer'].summary }}", ctx,
    ) == "Prev: hi"


def test_events_missing_step_renders_empty(tmp_path: Path) -> None:
    ctx = build_context(
        change_title="t",
        event_log=_log(tmp_path / "events.jsonl", []),
    )
    assert render_prompt("{{ events['nope'].field }}", ctx) == ""


def test_undefined_top_level_raises(tmp_path: Path) -> None:
    ctx = build_context(
        change_title="t",
        event_log=_log(tmp_path / "events.jsonl", []),
    )
    with pytest.raises(PromptRenderError):
        render_prompt("{{ bogus.thing }}", ctx)


def test_renders_vars_namespace(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(change_title="t", event_log=log,
                        vars={"fast_path": True})
    assert render_prompt("{{ vars.fast_path }}", ctx) == "True"
    assert render_prompt("{% if vars.fast_path %}fast{% endif %}", ctx) == "fast"


def test_vars_typo_raises_strict_within(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(change_title="t", event_log=log,
                        vars={"fast_path": False})
    with pytest.raises(PromptRenderError):
        render_prompt("{{ vars.fastpath }}", ctx)


def test_vars_default_empty_when_omitted(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(change_title="t", event_log=log)
    # An unknown var still raises (strict-within over an empty map).
    with pytest.raises(PromptRenderError):
        render_prompt("{{ vars.anything }}", ctx)


def test_coder_prompt_gates_tdd_on_fast_path(tmp_path: Path) -> None:
    """The shipped coder template must render two branches: fast_path
    waives tests; else loads TDD. StrictUndefined means both renders pass
    fast_path explicitly (there is no undefined->else fallback)."""
    from imp.assets import bundled_workflow_dir
    from imp.engine.loader import load_workflow

    wf = load_workflow(bundled_workflow_dir("imp"))
    body = wf.agents["coder"].prompt_template_source
    log = _log(tmp_path / "events.jsonl", [])

    fast = render_prompt(
        body,
        build_context(change_title="t", event_log=log,
                      vars={"fast_path": True}),
    )
    slow = render_prompt(
        body,
        build_context(change_title="t", event_log=log,
                      vars={"fast_path": False}),
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
            ("agent_output", {"status": "Success", "summary": "good"}),
        ],
    )
    # Simulate a crash mid-append: a partial JSON line, no trailing newline.
    with path.open("a") as f:
        f.write('{"type": "agent_output", "payload": {"summa')
    ctx = build_context(change_title="t", event_log=log)
    assert render_prompt(
        "Prev: {{ events['spec-writer'].summary }}", ctx,
    ) == "Prev: good"


def test_renders_project_facts(tmp_path: Path) -> None:
    ctx = build_context(
        change_title="t",
        event_log=_log(tmp_path / "events.jsonl", []),
        project_facts={"test_command": "make test"},
    )
    assert render_prompt("{{ project.test_command }}", ctx) == "make test"


def test_project_guidance_reference_raises(tmp_path: Path) -> None:
    # guidance is no longer a template fact: a stale reference fails loudly at
    # render (strict project namespace) instead of silently rendering empty.
    ctx = build_context(
        change_title="t",
        event_log=_log(tmp_path / "events.jsonl", []),
    )
    with pytest.raises(PromptRenderError):
        render_prompt("{{ project.guidance }}", ctx)


def test_missing_fact_attr_raises(tmp_path: Path) -> None:
    ctx = build_context(
        change_title="t",
        event_log=_log(tmp_path / "events.jsonl", []),
        project_facts={"a": "1"},
    )
    with pytest.raises(PromptRenderError):
        render_prompt("{{ project.absent }}", ctx)


def test_missing_fact_item_raises(tmp_path: Path) -> None:
    ctx = build_context(
        change_title="t",
        event_log=_log(tmp_path / "events.jsonl", []),
        project_facts={"a": "1"},
    )
    with pytest.raises(PromptRenderError):
        render_prompt('{{ project["absent"] }}', ctx)


def test_default_filter_does_not_rescue_strict_namespace(tmp_path: Path) -> None:
    ctx = build_context(
        change_title="t",
        event_log=_log(tmp_path / "events.jsonl", []),
        project_facts={"a": "1"},
    )
    with pytest.raises(PromptRenderError):
        render_prompt("{{ project.absent | default('x') }}", ctx)


def test_dunder_raises_attributeerror() -> None:
    from imp.agents.prompts import _ProjectNamespace
    ns = _ProjectNamespace({"a": "1", "guidance": ""})
    with pytest.raises(AttributeError):
        _ = ns.__html__


def test_renders_change_home(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(
        change_id="abc123", change_title="My Change",
        change_home="/ctl/.imp/changes/abc123", event_log=log,
    )
    assert render_prompt(
        "spec at {{ change.home }}/artifacts/spec/{{ change.id }}.md", ctx,
    ) == "spec at /ctl/.imp/changes/abc123/artifacts/spec/abc123.md"


def test_change_home_defaults_empty(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(change_id="t", change_title="t", event_log=log)
    assert render_prompt("{{ change.home }}", ctx) == ""



def test_missing_run_attr_raises(tmp_path: Path) -> None:
    ctx = build_context(
        change_title="t", event_log=_log(tmp_path / "events.jsonl", []),
    )
    with pytest.raises(PromptRenderError):
        render_prompt("{{ run.bogus }}", ctx)


def test_renders_cur_base_head(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(change_title="t", event_log=log, cur_base_head="deadbeef")
    assert render_prompt("{{ change.cur_base_head }}", ctx) == "deadbeef"


def test_cur_base_head_defaults_empty(tmp_path: Path) -> None:
    ctx = build_context(
        change_title="t", event_log=_log(tmp_path / "events.jsonl", []),
    )
    assert render_prompt("{{ change.cur_base_head }}", ctx) == ""


def test_render_resolves_include_with_workflow_dir(tmp_path: Path) -> None:
    (tmp_path / "partials").mkdir()
    (tmp_path / "partials" / "frag.md").write_text("SHARED-FRAGMENT\n")
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(change_title="t", event_log=log)
    out = render_prompt(
        '{% include "partials/frag.md" %}', ctx, workflow_dir=tmp_path)
    assert "SHARED-FRAGMENT" in out


def test_render_without_workflow_dir_cannot_resolve_include(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(change_title="t", event_log=log)
    with pytest.raises(PromptRenderError):
        render_prompt('{% include "partials/frag.md" %}', ctx)



def test_plain_gate_asks_build_affirmative(tmp_path: Path) -> None:
    src = Path(
        "src/imp/assets/workflows/imp/partials/brainstorm-dialogue.md"
    ).read_text()
    log = _log(tmp_path / "events.jsonl", [])
    rendered = render_prompt(
        src,
        build_context(change_title="t", event_log=log),
        workflow_dir=bundled_workflow_dir("imp"))
    assert "Want me to build this out now?" in rendered
    assert "save this to your backlog" not in rendered


def test_renders_run_autonomous_true(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(change_title="t", event_log=log, autonomous=True)
    assert render_prompt("{{ run.autonomous }}", ctx) == "True"
    assert render_prompt(
        "{% if run.autonomous %}take{% else %}confirm{% endif %}", ctx) == "take"


def test_renders_run_autonomous_false_default(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(change_title="t", event_log=log)
    assert render_prompt("{{ run.autonomous }}", ctx) == "False"
    assert render_prompt(
        "{% if run.autonomous %}take{% else %}confirm{% endif %}", ctx) == "confirm"


def test_change_namespace_exposes_orig_base_head(tmp_path) -> None:
    from imp.agents.prompts import build_context, render_prompt
    from imp.engine.state import JsonlEventLog

    log = JsonlEventLog(tmp_path / "events.jsonl")
    ctx = build_context(
        change_title="t", event_log=log, change_id="abc",
        cur_base_head="NEWTIP", orig_base_head="OLDTIP")
    # Both heads render, so a template can branch on whether the base moved.
    out = render_prompt(
        "{{ change.orig_base_head }}|{{ change.cur_base_head }}|"
        "{% if change.cur_base_head != change.orig_base_head %}MOVED{% else %}SAME{% endif %}",
        ctx)
    assert out == "OLDTIP|NEWTIP|MOVED"

    same = build_context(
        change_title="t", event_log=log, change_id="abc",
        cur_base_head="X", orig_base_head="X")
    out2 = render_prompt(
        "{% if change.cur_base_head != change.orig_base_head %}MOVED{% else %}SAME{% endif %}",
        same)
    assert out2 == "SAME"


def test_build_or_save_attended_build_default_yields(tmp_path: Path) -> None:
    src = Path(
        "src/imp/assets/workflows/imp/partials/brainstorm-dialogue.md"
    ).read_text()
    log = _log(tmp_path / "events.jsonl", [])
    rendered = render_prompt(
        src, build_context(change_title="t", event_log=log),
        workflow_dir=bundled_workflow_dir("imp"))
    assert "Want me to build this out now?" in rendered
    assert "end your turn to yield" in rendered
    assert "take the default and proceed without yielding" not in rendered
    assert "headless run" not in src
    assert "--non-interactive" not in rendered
    assert "run.non_interactive" not in src


def test_renders_run_opening_when_present(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(change_title="t", event_log=log,
                        opening="add a dark-mode toggle")
    assert ctx.run.opening == "add a dark-mode toggle"
    assert render_prompt(
        "{% if run.opening %}SEED:{{ run.opening }}{% else %}BARE{% endif %}",
        ctx) == "SEED:add a dark-mode toggle"


def test_run_opening_defaults_empty_and_selects_bare_branch(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(change_title="t", event_log=log)
    assert ctx.run.opening == ""
    assert render_prompt(
        "{% if run.opening %}SEED{% else %}BARE{% endif %}", ctx) == "BARE"


def test_step_namespace_defaults_to_lap_one(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(change_title="t", event_log=log)
    assert render_prompt("{{ step.lap }}", ctx) == "1"
    assert render_prompt("{{ step.prior_laps | length }}", ctx) == "0"


def test_step_namespace_lap_is_last_recorded_plus_one(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(
        change_title="t", event_log=log,
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
        change_title="t", event_log=log,
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
        "src/imp/assets/workflows/imp/partials/brainstorm-dialogue.md"
    ).read_text()
    log = _log(tmp_path / "events.jsonl", [])
    rendered = render_prompt(
        src, build_context(change_title="t", event_log=log, autonomous=True),
        workflow_dir=bundled_workflow_dir("imp"))
    assert "take the default and proceed without yielding" in rendered
    assert "Want me to build this out now?" not in rendered
    assert "create_and_enter_worktree" in rendered


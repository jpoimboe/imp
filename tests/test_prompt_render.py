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
        project_facts={"test_command": "make test"}, guidance="be careful",
    )
    assert render_prompt("{{ project.test_command }}", ctx) == "make test"
    assert render_prompt("{{ project.guidance }}", ctx) == "be careful"


def test_guidance_defaults_empty(tmp_path: Path) -> None:
    ctx = build_context(
        change_title="t",
        event_log=_log(tmp_path / "events.jsonl", []),
    )
    assert render_prompt("{{ project.guidance }}", ctx) == ""


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


def test_renders_run_namespace_true(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(change_title="t", event_log=log, non_interactive=True)
    assert render_prompt("{{ run.non_interactive }}", ctx) == "True"
    assert render_prompt(
        "{% if run.non_interactive %}headless{% else %}interactive{% endif %}", ctx,
    ) == "headless"


def test_renders_run_namespace_false_default(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(change_title="t", event_log=log)
    assert render_prompt("{{ run.non_interactive }}", ctx) == "False"
    assert render_prompt(
        "{% if run.non_interactive %}headless{% else %}interactive{% endif %}", ctx,
    ) == "interactive"


def test_renders_run_scope_only_true(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(change_title="t", event_log=log, scope_only=True)
    assert render_prompt("{{ run.scope_only }}", ctx) == "True"
    assert render_prompt(
        "{% if run.scope_only %}save{% else %}build{% endif %}", ctx) == "save"


def test_renders_run_scope_only_false_default(tmp_path: Path) -> None:
    log = _log(tmp_path / "events.jsonl", [])
    ctx = build_context(change_title="t", event_log=log)
    assert render_prompt("{{ run.scope_only }}", ctx) == "False"
    assert render_prompt(
        "{% if run.scope_only %}save{% else %}build{% endif %}", ctx) == "build"


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


def test_scope_only_gate_recommends_saving(tmp_path: Path) -> None:
    src = Path(
        "src/imp/assets/workflows/imp/partials/brainstorm-dialogue.md"
    ).read_text()
    log = _log(tmp_path / "events.jsonl", [])
    rendered = render_prompt(
        src, build_context(change_title="t", event_log=log, scope_only=True),
        workflow_dir=bundled_workflow_dir("imp"))
    assert "save this to your backlog" in rendered
    assert "Want me to build this out now?" not in rendered


def test_plain_gate_asks_build_affirmative(tmp_path: Path) -> None:
    src = Path(
        "src/imp/assets/workflows/imp/partials/brainstorm-dialogue.md"
    ).read_text()
    log = _log(tmp_path / "events.jsonl", [])
    rendered = render_prompt(
        src,
        build_context(change_title="t", event_log=log,
                      non_interactive=False, scope_only=False),
        workflow_dir=bundled_workflow_dir("imp"))
    assert "Want me to build this out now?" in rendered
    assert "save this to your backlog" not in rendered


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

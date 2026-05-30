"""Tests for the workflow loader."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from imp.engine.errors import LoadError, LoadErrors
from imp.engine.loader import load_workflow, parse_agent_file
from imp.engine.types import Goto, Pause


def write_agent_md(path: Path, frontmatter: str, body: str = "prompt body") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}\n")
    return path


# ── parse_agent_file tests ─────────────────────────────────────────────────


def test_parse_agent_minimal(tmp_path: Path) -> None:
    path = write_agent_md(
        tmp_path / "stub.md",
        frontmatter="""
name: stub
description: a stub agent
tools: [complete_step]
output:
  status: {type: enum, values: [Success, Error]}
""".strip(),
        body="Hello {{ change.id }}",
    )
    agent = parse_agent_file(path)
    assert agent.name == "stub"
    assert "complete_step" in agent.tools
    assert agent.output_schema.status_field() is not None
    assert "{{ change.id }}" in agent.prompt_template_source


def test_parse_agent_subagents(tmp_path: Path) -> None:
    path = write_agent_md(
        tmp_path / "stub.md",
        frontmatter="""
name: stub
tools: [complete_step, task]
subagents: [reviewer, summarizer]
output:
  status: {type: enum, values: [Success, Error]}
""".strip(),
    )
    agent = parse_agent_file(path)
    assert agent.subagents == ("reviewer", "summarizer")


def test_parse_agent_missing_frontmatter(tmp_path: Path) -> None:
    p = tmp_path / "broken.md"
    p.write_text("no frontmatter here\n")
    with pytest.raises(LoadError) as ei:
        parse_agent_file(p)
    assert "frontmatter" in str(ei.value).lower()


def test_parse_agent_missing_name(tmp_path: Path) -> None:
    p = write_agent_md(
        tmp_path / "noname.md",
        frontmatter=(
            "tools: [complete_step]\noutput:\n  "
            "status: {type: enum, values: [Success, Error]}"
        ),
    )
    with pytest.raises(LoadError) as ei:
        parse_agent_file(p)
    assert "missing 'name'" in str(ei.value)


def test_parse_agent_missing_output(tmp_path: Path) -> None:
    p = write_agent_md(
        tmp_path / "noout.md",
        frontmatter="name: x\ntools: [complete_step]",
    )
    with pytest.raises(LoadError) as ei:
        parse_agent_file(p)
    assert "missing 'output'" in str(ei.value)


def test_parse_agent_invalid_yaml(tmp_path: Path) -> None:
    p = tmp_path / "bad.md"
    p.write_text("---\nname: [unclosed\n---\nbody\n")
    with pytest.raises(LoadError) as ei:
        parse_agent_file(p)
    assert "yaml" in str(ei.value).lower()


def test_parse_agent_tools_null(tmp_path: Path) -> None:
    """`tools: null` should be treated as empty, not crash."""
    p = write_agent_md(
        tmp_path / "x.md",
        frontmatter=(
            "name: x\ntools: null\n"
            "output:\n  status: {type: enum, values: [Success, Error]}"
        ),
    )
    agent = parse_agent_file(p)
    assert agent.tools == frozenset()


def test_parse_agent_subagents_null(tmp_path: Path) -> None:
    p = write_agent_md(
        tmp_path / "x.md",
        frontmatter=(
            "name: x\ntools: [complete_step]\nsubagents: null\n"
            "output:\n  status: {type: enum, values: [Success, Error]}"
        ),
    )
    agent = parse_agent_file(p)
    assert agent.subagents == ()


def test_parse_agent_output_not_a_mapping(tmp_path: Path) -> None:
    p = write_agent_md(
        tmp_path / "x.md",
        frontmatter="name: x\ntools: [complete_step]\noutput: null",
    )
    with pytest.raises(LoadError) as ei:
        parse_agent_file(p)
    assert "'output' must be a mapping" in str(ei.value)


def test_parse_agent_output_as_list(tmp_path: Path) -> None:
    p = write_agent_md(
        tmp_path / "x.md",
        frontmatter="name: x\ntools: [complete_step]\noutput: [1, 2, 3]",
    )
    with pytest.raises(LoadError) as ei:
        parse_agent_file(p)
    assert "'output' must be a mapping" in str(ei.value)


def test_parse_agent_crlf_line_endings(tmp_path: Path) -> None:
    """Files saved with CRLF must parse correctly with normalized body."""
    p = tmp_path / "crlf.md"
    p.write_bytes(
        b"---\r\nname: stub\r\ntools: [complete_step]\r\n"
        b"output:\r\n  status: {type: enum, values: [Success, Error]}\r\n"
        b"---\r\nHello {{ change.id }}\r\n"
    )
    agent = parse_agent_file(p)
    assert agent.name == "stub"
    assert "\r" not in agent.prompt_template_source
    assert "Hello {{ change.id }}" in agent.prompt_template_source


# ── Workflow helper ────────────────────────────────────────────────────────


def _write(root: Path, workflow_yaml: str, agents: dict[str, str] | None = None) -> Path:
    wf = root / "wf"
    (wf / "agents").mkdir(parents=True)
    (wf / "workflow.yaml").write_text(textwrap.dedent(workflow_yaml))
    for stem, body in (agents or {}).items():
        (wf / "agents" / f"{stem}.md").write_text(textwrap.dedent(body))
    return wf


_REVIEWER = """\
---
name: spec-reviewer
tools: [complete_step]
output:
  status: {type: enum, values: [Success, Error]}
  verdict: {type: enum, values: [approve, rework]}
---
review
"""

_WRITER = """\
---
name: spec-writer
tools: [complete_step]
output:
  status: {type: enum, values: [Success, Error]}
---
write
"""


# ── New inline phase surface tests ────────────────────────────────────────


def test_inline_phase_surface_and_kind_inference(tmp_path: Path) -> None:
    wf = _write(tmp_path, """\
        name: t
        description: t
        vars:
          review_done: {type: bool, default: false}
        phases:
          - name: planning
            steps:
              - name: spec-writer
                file: agents/spec-writer.md
                context: resume
              - name: route
                rules:
                  - {if: review_done, goto: implement}
                  - {else: true, continue: true}
              - name: spec-reviewer
                file: agents/spec-reviewer.md
                max_loops: 6
                rules:
                  - {if: {verdict: rework}, goto: spec-writer}
                  - if: {verdict: approve}
                    goto: spec-writer
                    set: {review_done: true}
                    feedback: false
                  - {else: true, pause: true}
          - name: implement
            steps:
              - name: coder
                file: agents/spec-writer.md
          - name: merge
            steps: []
        """, agents={"spec-writer": _WRITER, "spec-reviewer": _REVIEWER})
    wf_obj = load_workflow(wf)
    names = [s.name for s in wf_obj.flat_steps()]
    assert names == ["spec-writer", "route", "spec-reviewer", "coder"]
    by = {s.name: s for s in wf_obj.flat_steps()}
    assert by["spec-writer"].kind == "agent"
    assert by["spec-writer"].context == "resume"
    assert by["spec-writer"].phase == "planning"
    assert by["route"].kind == "branch"
    assert by["spec-reviewer"].kind == "agent"
    assert by["spec-reviewer"].max_loops == 6
    approve = by["spec-reviewer"].rules[1]
    assert isinstance(approve.action, Goto)
    assert approve.action.feedback is False
    assert wf_obj.vars.defaults() == {"review_done": False}


def test_if_string_unquoted_identifier_is_load_error(tmp_path: Path) -> None:
    wf = _write(tmp_path, """\
        name: t
        description: t
        phases:
          - name: only
            steps:
              - name: r
                file: agents/spec-reviewer.md
                rules:
                  - {if: "verdict == rework", continue: true}
                  - {else: true, pause: true}
        """, agents={"spec-reviewer": _REVIEWER})
    with pytest.raises((LoadError, LoadErrors)):
        load_workflow(wf)


def test_if_must_be_bool(tmp_path: Path) -> None:
    wf = _write(tmp_path, """\
        name: t
        description: t
        vars:
          round: {type: int, default: 0}
        phases:
          - name: only
            steps:
              - name: b
                rules:
                  - {if: round, continue: true}
                  - {else: true, pause: true}
        """)
    with pytest.raises((LoadError, LoadErrors)):
        load_workflow(wf)


def test_set_type_mismatch_is_load_error(tmp_path: Path) -> None:
    wf = _write(tmp_path, """\
        name: t
        description: t
        vars:
          round: {type: int, default: 0}
        phases:
          - name: only
            steps:
              - name: b
                rules:
                  - {else: true, continue: true, set: {round: true}}
        """)
    with pytest.raises((LoadError, LoadErrors)):
        load_workflow(wf)


def test_two_kind_signals_is_load_error(tmp_path: Path) -> None:
    wf = _write(tmp_path, """\
        name: t
        description: t
        phases:
          - name: only
            steps:
              - name: bad
                command: "true"
                prompt: "do it"
                rules:
                  - {else: true, continue: true}
        """)
    with pytest.raises((LoadError, LoadErrors)):
        load_workflow(wf)


def test_human_option_set_type_mismatch_is_load_error(tmp_path: Path) -> None:
    # A human option's set: is type-checked just like a rule's set:.
    wf = _write(tmp_path, """\
        name: t
        description: t
        vars:
          round: {type: int, default: 0}
        phases:
          - name: only
            steps:
              - name: gate
                ask: "Proceed?"
                options:
                  - {label: Go, continue: true, set: {round: true}}   # bool into int var
        """)
    with pytest.raises((LoadError, LoadErrors)):
        load_workflow(wf)


# ── Error path tests for inline surface ──────────────────────────────────


def test_load_workflow_missing_phases_key(tmp_path: Path) -> None:
    wf_dir = tmp_path / "wf"
    (wf_dir / "agents").mkdir(parents=True)
    (wf_dir / "workflow.yaml").write_text("name: x\ndescription: y\n")
    with pytest.raises(LoadError) as ei:
        load_workflow(wf_dir)
    assert "missing 'phases'" in str(ei.value)


def test_load_workflow_duplicate_agent_name(tmp_path: Path) -> None:
    wf_dir = tmp_path / "wf"
    (wf_dir / "agents").mkdir(parents=True)
    write_agent_md(
        wf_dir / "agents" / "alpha.md",
        frontmatter=(
            "name: reviewer\ntools: [complete_step]\n"
            "output:\n  status: {type: enum, values: [Success, Error]}"
        ),
    )
    write_agent_md(
        wf_dir / "agents" / "beta.md",
        frontmatter=(
            "name: reviewer\ntools: [complete_step]\n"
            "output:\n  status: {type: enum, values: [Success, Error]}"
        ),
    )
    (wf_dir / "workflow.yaml").write_text(
        "name: x\ndescription: \"\"\nphases:\n  - name: p\n    steps: []\n"
    )
    with pytest.raises(LoadError) as ei:
        load_workflow(wf_dir)
    assert "duplicate agent name" in str(ei.value)


def test_load_defaults_fallback(tmp_path: Path) -> None:
    wf = _write(tmp_path, """\
        name: x
        description: y
        phases:
          - name: p
            steps:
              - name: s
                file: agents/spec-writer.md
        """, agents={"spec-writer": _WRITER})
    wf_obj = load_workflow(wf)
    assert wf_obj.defaults.max_retries == 3       # built-in default
    assert isinstance(wf_obj.defaults.on_max_retries, Pause)


def test_load_workflow_max_retries_string(tmp_path: Path) -> None:
    """max_retries: 'three' should raise LoadError, not TypeError."""
    wf = _write(tmp_path, """\
        name: x
        description: ""
        defaults: {max_retries: "three"}
        phases:
          - name: p
            steps:
              - name: s
                file: agents/spec-writer.md
        """, agents={"spec-writer": _WRITER})
    with pytest.raises((LoadError, LoadErrors)) as ei:
        load_workflow(wf)
    assert "max_retries" in str(ei.value) and "must be an integer" in str(ei.value)


def test_load_workflow_steps_null(tmp_path: Path) -> None:
    """steps: null should raise LoadError, not TypeError."""
    wf_dir = tmp_path / "wf"
    (wf_dir / "agents").mkdir(parents=True)
    (wf_dir / "workflow.yaml").write_text(
        "name: x\ndescription: \"\"\nphases:\n  - name: p\n    steps: null\n"
    )
    with pytest.raises((LoadError, LoadErrors)) as ei:
        load_workflow(wf_dir)
    assert "'steps' must be a list" in str(ei.value)


def test_load_workflow_step_name_null(tmp_path: Path) -> None:
    wf_dir = tmp_path / "wf"
    (wf_dir / "agents").mkdir(parents=True)
    (wf_dir / "workflow.yaml").write_text(
        "name: x\ndescription: \"\"\nphases:\n  - name: p\n    steps:\n"
        "      - {name: null, file: agents/stub.md}\n"
    )
    with pytest.raises((LoadError, LoadErrors)) as ei:
        load_workflow(wf_dir)
    assert "missing" in str(ei.value).lower() and "name" in str(ei.value).lower()


def test_load_defaults_max_tool_calls(tmp_path: Path) -> None:
    wf = _write(tmp_path, """\
        name: t
        description: t
        defaults: {max_retries: 1, max_tool_calls: 25}
        phases:
          - name: only
            steps:
              - name: w
                file: agents/spec-writer.md
        """, agents={"spec-writer": _WRITER})
    w = load_workflow(wf)
    assert w.defaults.max_tool_calls == 25


def test_load_defaults_max_tool_calls_optional(tmp_path: Path) -> None:
    wf = _write(tmp_path, """\
        name: t
        description: t
        defaults: {max_retries: 1}
        phases:
          - name: only
            steps:
              - name: w
                file: agents/spec-writer.md
        """, agents={"spec-writer": _WRITER})
    w = load_workflow(wf)
    assert w.defaults.max_tool_calls is None


def test_step_source_line_is_accurate(tmp_path: Path) -> None:
    wf_dir = tmp_path / "wf"
    (wf_dir / "agents").mkdir(parents=True)
    write_agent_md(
        wf_dir / "agents" / "spec-writer.md",
        frontmatter=(
            "name: spec-writer\ntools: [complete_step]\n"
            "output:\n  status: {type: enum, values: [Success, Error]}"
        ),
    )
    wf_yaml = wf_dir / "workflow.yaml"
    wf_yaml.write_text(
        "name: t\n"                      # line 1
        "description: \"\"\n"            # line 2
        "phases:\n"                      # line 3
        "  - name: p\n"                  # line 4
        "    steps:\n"                   # line 5
        "      - name: s1\n"             # line 6
        "        file: agents/spec-writer.md\n"  # line 7
    )
    wf = load_workflow(wf_dir)
    step = wf.phases[0].steps[0]
    assert step.source.line == 6


def test_error_includes_source_line(tmp_path: Path) -> None:
    wf_dir = tmp_path / "wf"
    (wf_dir / "agents").mkdir(parents=True)
    wf_yaml = wf_dir / "workflow.yaml"
    wf_yaml.write_text(
        "name: t\n"
        "description: \"\"\n"
        "phases:\n"
        "  - name: p\n"
        "    steps:\n"
        "      - file: agents/missing.md\n"  # no 'name' — line 6
    )
    with pytest.raises((LoadError, LoadErrors)) as ei:
        load_workflow(wf_dir)
    assert ":6" in str(ei.value)

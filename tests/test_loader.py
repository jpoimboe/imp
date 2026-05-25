from pathlib import Path

import pytest

from imp.engine.errors import LoadError
from imp.engine.loader import load_workflow, parse_agent_file
from imp.engine.types import Goto, Pause, ScriptGate
from tests.conftest import write_agent_md


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


# ── Workflow tests (Task 5) ────────────────────────────────────────────────


def _write_min_workflow(root: Path) -> Path:
    """Writes a minimal workflow with one empty phase 'onboard'."""
    wf_dir = root / ".imp" / "workflows" / "default"
    (wf_dir / "agents").mkdir(parents=True)
    write_agent_md(
        wf_dir / "agents" / "stub.md",
        frontmatter=(
            "name: stub\ntools: [complete_step]\n"
            "output:\n  status: {type: enum, values: [Success, Error]}"
        ),
    )
    (wf_dir / "workflow.yaml").write_text(
        """
name: test
description: a minimal workflow
phases: [onboard]

defaults:
  max_retries: 5
  on_max_retries: {kind: pause}

onboard:
  steps:
    - {name: s1, file: agents/stub.md}
""".strip()
    )
    return wf_dir


def test_load_workflow_basic(tmp_path: Path) -> None:
    wf_dir = _write_min_workflow(tmp_path)
    wf = load_workflow(wf_dir)
    assert wf.name == "test"
    assert tuple(p.name for p in wf.phases) == ("onboard",)
    assert wf.defaults.max_retries == 5
    assert isinstance(wf.defaults.on_max_retries, Pause)


def test_load_workflow_missing_phases_key(tmp_path: Path) -> None:
    wf_dir = tmp_path / "wf"
    (wf_dir / "agents").mkdir(parents=True)
    (wf_dir / "workflow.yaml").write_text("name: x\ndescription: y\n")
    with pytest.raises(LoadError) as ei:
        load_workflow(wf_dir)
    assert "missing 'phases'" in str(ei.value)


def test_load_workflow_phase_not_defined(tmp_path: Path) -> None:
    wf_dir = tmp_path / "wf"
    (wf_dir / "agents").mkdir(parents=True)
    (wf_dir / "workflow.yaml").write_text(
        "name: x\ndescription: y\nphases: [a, b]\n\na:\n  steps: []\n"
    )
    with pytest.raises(LoadError) as ei:
        load_workflow(wf_dir)
    assert "phase 'b' listed in phases: but no 'b:' section" in str(ei.value)


def test_load_defaults_fallback(tmp_path: Path) -> None:
    wf_dir = tmp_path / "wf"
    (wf_dir / "agents").mkdir(parents=True)
    write_agent_md(
        wf_dir / "agents" / "stub.md",
        frontmatter=(
            "name: stub\ntools: [complete_step]\n"
            "output:\n  status: {type: enum, values: [Success, Error]}"
        ),
    )
    (wf_dir / "workflow.yaml").write_text(
        "name: x\ndescription: y\nphases: [p]\n"
        "p:\n  steps:\n    - {name: s, file: agents/stub.md}\n"
    )
    wf = load_workflow(wf_dir)
    assert wf.defaults.max_retries == 3       # built-in default
    assert isinstance(wf.defaults.on_max_retries, Pause)


def test_load_defaults_goto(tmp_path: Path) -> None:
    wf_dir = tmp_path / "wf"
    (wf_dir / "agents").mkdir(parents=True)
    write_agent_md(
        wf_dir / "agents" / "stub.md",
        frontmatter=(
            "name: stub\ntools: [complete_step]\n"
            "output:\n  status: {type: enum, values: [Success, Error]}"
        ),
    )
    (wf_dir / "workflow.yaml").write_text(
        """
name: x
description: y
phases: [p1, p2]

defaults:
  max_retries: 2
  on_max_retries: {kind: goto, target: p1}

p1:
  steps:
    - {name: s, file: agents/stub.md}
p2:
  steps:
    - {name: s, file: agents/stub.md}
""".strip()
    )
    wf = load_workflow(wf_dir)
    assert isinstance(wf.defaults.on_max_retries, Goto)
    assert wf.defaults.on_max_retries.target == "p1"


def test_load_workflow_max_retries_null(tmp_path: Path) -> None:
    """max_retries: null should fall back to default, not crash."""
    wf_dir = tmp_path / "wf"
    (wf_dir / "agents").mkdir(parents=True)
    write_agent_md(
        wf_dir / "agents" / "stub.md",
        frontmatter=(
            "name: stub\ntools: [complete_step]\n"
            "output:\n  status: {type: enum, values: [Success, Error]}"
        ),
    )
    (wf_dir / "workflow.yaml").write_text(
        "name: x\ndescription: \"\"\nphases: [p]\n"
        "defaults: {max_retries: null}\n"
        "p:\n  steps:\n    - {name: s, file: agents/stub.md}\n"
    )
    wf = load_workflow(wf_dir)
    assert wf.defaults.max_retries == 3  # built-in default


def test_load_workflow_max_retries_string(tmp_path: Path) -> None:
    """max_retries: 'three' should raise LoadError, not TypeError."""
    wf_dir = tmp_path / "wf"
    (wf_dir / "agents").mkdir(parents=True)
    write_agent_md(
        wf_dir / "agents" / "stub.md",
        frontmatter=(
            "name: stub\ntools: [complete_step]\n"
            "output:\n  status: {type: enum, values: [Success, Error]}"
        ),
    )
    (wf_dir / "workflow.yaml").write_text(
        "name: x\ndescription: \"\"\nphases: [p]\n"
        "defaults: {max_retries: \"three\"}\n"
        "p:\n  steps:\n    - {name: s, file: agents/stub.md}\n"
    )
    with pytest.raises(LoadError) as ei:
        load_workflow(wf_dir)
    assert "max_retries" in str(ei.value) and "must be an integer" in str(ei.value)


def test_load_workflow_steps_null(tmp_path: Path) -> None:
    """steps: null should raise LoadError, not TypeError."""
    wf_dir = tmp_path / "wf"
    (wf_dir / "agents").mkdir(parents=True)
    (wf_dir / "workflow.yaml").write_text(
        "name: x\ndescription: \"\"\nphases: [p]\np:\n  steps: null\n"
    )
    with pytest.raises(LoadError) as ei:
        load_workflow(wf_dir)
    assert "'steps' must be a list" in str(ei.value)


def test_load_workflow_step_name_null(tmp_path: Path) -> None:
    wf_dir = tmp_path / "wf"
    (wf_dir / "agents").mkdir(parents=True)
    (wf_dir / "workflow.yaml").write_text(
        "name: x\ndescription: \"\"\nphases: [p]\n"
        "p:\n  steps:\n    - {name: null, file: agents/stub.md}\n"
    )
    with pytest.raises(LoadError) as ei:
        load_workflow(wf_dir)
    assert "missing 'name'" in str(ei.value)


def test_load_workflow_step_file_null(tmp_path: Path) -> None:
    wf_dir = tmp_path / "wf"
    (wf_dir / "agents").mkdir(parents=True)
    (wf_dir / "workflow.yaml").write_text(
        "name: x\ndescription: \"\"\nphases: [p]\n"
        "p:\n  steps:\n    - {name: s, file: null}\n"
    )
    with pytest.raises(LoadError) as ei:
        load_workflow(wf_dir)
    assert "missing 'file'" in str(ei.value)


def test_load_workflow_step_model_role_null(tmp_path: Path) -> None:
    """model_role: null should fall back to 'default', not the string 'None'."""
    wf_dir = tmp_path / "wf"
    (wf_dir / "agents").mkdir(parents=True)
    write_agent_md(
        wf_dir / "agents" / "stub.md",
        frontmatter=(
            "name: stub\ntools: [complete_step]\n"
            "output:\n  status: {type: enum, values: [Success, Error]}"
        ),
    )
    (wf_dir / "workflow.yaml").write_text(
        "name: x\ndescription: \"\"\nphases: [p]\n"
        "p:\n  steps:\n    - {name: s, file: agents/stub.md, model_role: null}\n"
    )
    wf = load_workflow(wf_dir)
    assert wf.phases[0].steps[0].model_role == "default"


def test_load_workflow_duplicate_phase_name(tmp_path: Path) -> None:
    wf_dir = tmp_path / "wf"
    (wf_dir / "agents").mkdir(parents=True)
    write_agent_md(
        wf_dir / "agents" / "stub.md",
        frontmatter=(
            "name: stub\ntools: [complete_step]\n"
            "output:\n  status: {type: enum, values: [Success, Error]}"
        ),
    )
    (wf_dir / "workflow.yaml").write_text(
        "name: x\ndescription: \"\"\nphases: [p, p]\n"
        "p:\n  steps:\n    - {name: s, file: agents/stub.md}\n"
    )
    with pytest.raises(LoadError) as ei:
        load_workflow(wf_dir)
    assert "duplicate phase" in str(ei.value)


def test_load_workflow_phases_null_entry(tmp_path: Path) -> None:
    wf_dir = tmp_path / "wf"
    (wf_dir / "agents").mkdir(parents=True)
    (wf_dir / "workflow.yaml").write_text(
        "name: x\ndescription: \"\"\nphases: [null]\n"
    )
    with pytest.raises(LoadError) as ei:
        load_workflow(wf_dir)
    assert "null entry" in str(ei.value)


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
        "name: x\ndescription: \"\"\nphases: [p]\n"
        "p:\n  steps:\n    - {name: s, file: agents/reviewer.md}\n"
    )
    with pytest.raises(LoadError) as ei:
        load_workflow(wf_dir)
    assert "duplicate agent name" in str(ei.value)


# ── Task 6: Step gates + fs_permissions ───────────────────────────────────


def test_load_step_with_script_gate(tmp_path: Path) -> None:
    wf_dir = tmp_path / "wf"
    (wf_dir / "agents").mkdir(parents=True)
    write_agent_md(
        wf_dir / "agents" / "stub.md",
        frontmatter=(
            "name: stub\ntools: [complete_step]\n"
            "output:\n  status: {type: enum, values: [Success, Error]}"
        ),
    )
    (wf_dir / "workflow.yaml").write_text(
        """
name: t
description: ""
phases: [p]
p:
  steps:
    - name: s
      file: agents/stub.md
      gates:
        - type: script
          command: "make test"
          decide:
            - {when: {exit: 0}, action: {kind: continue}}
            - {else: true, action: {kind: retry}}
""".strip()
    )
    wf = load_workflow(wf_dir)
    step = wf.phases[0].steps[0]
    assert len(step.gates) == 1
    g = step.gates[0]
    assert isinstance(g, ScriptGate)
    assert g.command == "make test"


def test_load_step_with_fs_permissions(tmp_path: Path) -> None:
    wf_dir = tmp_path / "wf"
    (wf_dir / "agents").mkdir(parents=True)
    write_agent_md(
        wf_dir / "agents" / "stub.md",
        frontmatter=(
            "name: stub\ntools: [complete_step]\n"
            "output:\n  status: {type: enum, values: [Success, Error]}"
        ),
    )
    (wf_dir / "workflow.yaml").write_text(
        """
name: t
description: ""
phases: [p]
p:
  steps:
    - name: s
      file: agents/stub.md
      fs_permissions:
        - {operations: [read],  paths: ["/project/**"], mode: allow}
        - {operations: [write], paths: ["/scratch/**"], mode: allow}
        - {operations: [write], paths: ["/**"], mode: deny}
""".strip()
    )
    wf = load_workflow(wf_dir)
    step = wf.phases[0].steps[0]
    assert len(step.fs_permissions) == 3
    assert step.fs_permissions[0].operations == ("read",)
    assert step.fs_permissions[2].mode == "deny"

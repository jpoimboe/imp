from pathlib import Path

import pytest

from imp.engine.errors import LoadErrors
from imp.engine.loader import load_workflow


def _make_workflow(
    tmp: Path,
    agents: dict[str, str] | None = None,
    workflow_yaml: str = "",
) -> Path:
    """Write a workflow dir; `agents` maps filename → frontmatter+body."""
    wf_dir = tmp / "wf"
    (wf_dir / "agents").mkdir(parents=True)
    for fname, content in (agents or {}).items():
        (wf_dir / "agents" / fname).write_text(content)
    (wf_dir / "workflow.yaml").write_text(workflow_yaml)
    return wf_dir


_OK_AGENT = (
    "---\n"
    "name: stub\n"
    "tools: [complete_step]\n"
    "output:\n"
    "  status: {type: enum, values: [Success, Error]}\n"
    "---\nprompt body\n"
)


def test_v1_step_file_missing(tmp_path: Path) -> None:
    wf = _make_workflow(
        tmp_path,
        agents={},  # no agents
        workflow_yaml=(
            "name: t\ndescription: \"\"\nphases: [p]\n"
            "p:\n  steps:\n    - {name: s, file: agents/missing.md}\n"
        ),
    )
    with pytest.raises(LoadErrors) as ei:
        load_workflow(wf)
    assert any("agent file not found" in str(e) for e in ei.value.errors)


def test_v2_status_missing(tmp_path: Path) -> None:
    bad = (
        "---\nname: bad\ntools: [complete_step]\n"
        "output:\n  summary: {type: string}\n"  # no 'status' field
        "---\nbody\n"
    )
    wf = _make_workflow(
        tmp_path,
        agents={"bad.md": bad},
        workflow_yaml=(
            "name: t\ndescription: \"\"\nphases: [p]\n"
            "p:\n  steps:\n    - {name: s, file: agents/bad.md}\n"
        ),
    )
    with pytest.raises(LoadErrors) as ei:
        load_workflow(wf)
    assert any("must declare 'status' as enum" in str(e) for e in ei.value.errors)


def test_v3_complete_step_missing(tmp_path: Path) -> None:
    bad = (
        "---\nname: bad\ntools: [read_file]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "---\nbody\n"
    )
    wf = _make_workflow(
        tmp_path,
        agents={"bad.md": bad},
        workflow_yaml=(
            "name: t\ndescription: \"\"\nphases: [p]\n"
            "p:\n  steps:\n    - {name: s, file: agents/bad.md}\n"
        ),
    )
    with pytest.raises(LoadErrors) as ei:
        load_workflow(wf)
    assert any("'complete_step' must be in tools" in str(e) for e in ei.value.errors)


def test_v4_decide_when_unknown_field(tmp_path: Path) -> None:
    agent_md = (
        "---\nname: reviewer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "---\nbody\n"
    )
    stub_md = (
        "---\nname: stub\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "---\nbody\n"
    )
    wf = _make_workflow(
        tmp_path,
        agents={"reviewer.md": agent_md, "stub.md": stub_md},
        workflow_yaml=(
            "name: t\ndescription: \"\"\nphases: [p]\n"
            "p:\n"
            "  steps: [{name: s, file: agents/stub.md}]\n"
            "  gates:\n"
            "    - type: agent\n"
            "      file: agents/reviewer.md\n"
            "      decide:\n"
            "        - {when: {nonexistent: 1}, action: {kind: continue}}\n"
        ),
    )
    with pytest.raises(LoadErrors) as ei:
        load_workflow(wf)
    assert any("'when.nonexistent'" in str(e) for e in ei.value.errors)


def test_v5_goto_unknown_phase(tmp_path: Path) -> None:
    wf = _make_workflow(
        tmp_path,
        agents={"stub.md": _OK_AGENT},
        workflow_yaml=(
            "name: t\ndescription: \"\"\nphases: [p1]\n"
            "p1:\n"
            "  steps: [{name: s, file: agents/stub.md}]\n"
            "  gates:\n"
            "    - type: human\n"
            "      prompt: 'go'\n"
            "      options:\n"
            "        - {label: yes, action: {kind: goto, target: missing}}\n"
        ),
    )
    with pytest.raises(LoadErrors) as ei:
        load_workflow(wf)
    assert any("goto target 'missing'" in str(e) for e in ei.value.errors)


def test_v7_human_gate_empty_options(tmp_path: Path) -> None:
    wf = _make_workflow(
        tmp_path,
        agents={"stub.md": _OK_AGENT},
        workflow_yaml=(
            "name: t\ndescription: \"\"\nphases: [p]\n"
            "p:\n"
            "  steps: [{name: s, file: agents/stub.md}]\n"
            "  gates:\n"
            "    - {type: human, prompt: q, options: []}\n"
        ),
    )
    with pytest.raises(LoadErrors) as ei:
        load_workflow(wf)
    assert any("at least one option" in str(e) for e in ei.value.errors)


def test_v8_duplicate_step_names(tmp_path: Path) -> None:
    wf = _make_workflow(
        tmp_path,
        agents={"stub.md": _OK_AGENT},
        workflow_yaml=(
            "name: t\ndescription: \"\"\nphases: [p]\n"
            "p:\n"
            "  steps:\n"
            "    - {name: s, file: agents/stub.md}\n"
            "    - {name: s, file: agents/stub.md}\n"
        ),
    )
    with pytest.raises(LoadErrors) as ei:
        load_workflow(wf)
    assert any("duplicate step name" in str(e) for e in ei.value.errors)


def test_v9_on_max_retries_is_retry(tmp_path: Path) -> None:
    wf = _make_workflow(
        tmp_path,
        agents={"stub.md": _OK_AGENT},
        workflow_yaml=(
            "name: t\ndescription: \"\"\nphases: [p]\n"
            "defaults:\n"
            "  max_retries: 2\n"
            "  on_max_retries: {kind: retry}\n"
            "p:\n  steps: [{name: s, file: agents/stub.md}]\n"
        ),
    )
    with pytest.raises(LoadErrors) as ei:
        load_workflow(wf)
    assert any("cannot itself be 'retry'" in str(e) for e in ei.value.errors)


def test_v11_fs_permissions_bad_paths(tmp_path: Path) -> None:
    wf = _make_workflow(
        tmp_path,
        agents={"stub.md": _OK_AGENT},
        workflow_yaml=(
            "name: t\ndescription: \"\"\nphases: [p]\n"
            "p:\n"
            "  steps:\n"
            "    - name: s\n"
            "      file: agents/stub.md\n"
            "      fs_permissions:\n"
            "        - {operations: [read], paths: ['../escape'], mode: allow}\n"
        ),
    )
    with pytest.raises(LoadErrors) as ei:
        load_workflow(wf)
    assert any("'..'" in str(e) for e in ei.value.errors)


def test_v14_filename_must_match_name(tmp_path: Path) -> None:
    """agents/coder.md declaring name: not-coder → error."""
    mismatched = (
        "---\nname: not-coder\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "---\nbody\n"
    )
    wf = _make_workflow(
        tmp_path,
        agents={"coder.md": mismatched},
        workflow_yaml=(
            "name: t\ndescription: \"\"\nphases: [p]\n"
            "p:\n  steps: [{name: s, file: agents/coder.md}]\n"
        ),
    )
    with pytest.raises(LoadErrors) as ei:
        load_workflow(wf)
    assert any("filename stem is 'coder'" in str(e) for e in ei.value.errors)


def test_v13_warning_decide_no_else(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    agent_md = (
        "---\nname: reviewer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "---\nbody\n"
    )
    wf = _make_workflow(
        tmp_path,
        agents={"reviewer.md": agent_md, "stub.md": _OK_AGENT},
        workflow_yaml=(
            "name: t\ndescription: \"\"\nphases: [p]\n"
            "p:\n"
            "  steps: [{name: s, file: agents/stub.md}]\n"
            "  gates:\n"
            "    - type: agent\n"
            "      file: agents/reviewer.md\n"
            "      decide:\n"
            "        - {when: {status: Success}, action: {kind: continue}}\n"
        ),
    )
    load_workflow(wf)  # should not raise — warning only
    captured = capsys.readouterr()
    assert "no terminal 'else: true'" in captured.err


def test_v1_step_gate_agent_file_missing(tmp_path: Path) -> None:
    """AgentGate inside a step's gates should also have its file checked."""
    wf = _make_workflow(
        tmp_path,
        agents={"stub.md": _OK_AGENT},
        workflow_yaml=(
            "name: t\ndescription: \"\"\nphases: [p]\n"
            "p:\n  steps:\n"
            "    - name: s\n"
            "      file: agents/stub.md\n"
            "      gates:\n"
            "        - type: agent\n"
            "          file: agents/missing.md\n"
            "          decide:\n"
            "            - {else: true, action: {kind: continue}}\n"
        ),
    )
    with pytest.raises(LoadErrors) as ei:
        load_workflow(wf)
    assert any("agent gate file not found" in str(e) for e in ei.value.errors)


def test_v11_step_gate_agent_fs_permissions(tmp_path: Path) -> None:
    """AgentGate's fs_permissions inside step.gates should be validated."""
    reviewer = (
        "---\nname: reviewer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "---\nbody\n"
    )
    wf = _make_workflow(
        tmp_path,
        agents={"stub.md": _OK_AGENT, "reviewer.md": reviewer},
        workflow_yaml=(
            "name: t\ndescription: \"\"\nphases: [p]\n"
            "p:\n  steps:\n"
            "    - name: s\n"
            "      file: agents/stub.md\n"
            "      gates:\n"
            "        - type: agent\n"
            "          file: agents/reviewer.md\n"
            "          fs_permissions:\n"
            "            - {operations: [read], paths: ['../escape'], mode: allow}\n"
            "          decide:\n"
            "            - {else: true, action: {kind: continue}}\n"
        ),
    )
    with pytest.raises(LoadErrors) as ei:
        load_workflow(wf)
    assert any("'..'" in str(e) for e in ei.value.errors)


def test_v11_dotted_filename_allowed(tmp_path: Path) -> None:
    """Paths with '..' inside a filename (not as a path component) should not be flagged."""
    wf = _make_workflow(
        tmp_path,
        agents={"stub.md": _OK_AGENT},
        workflow_yaml=(
            "name: t\ndescription: \"\"\nphases: [p]\n"
            "p:\n  steps:\n"
            "    - name: s\n"
            "      file: agents/stub.md\n"
            "      fs_permissions:\n"
            "        - {operations: [read], paths: ['/var/log/app..log'], mode: allow}\n"
        ),
    )
    load_workflow(wf)  # no raise


def test_v12_step_gate_agent_model_role_unconfigured(tmp_path: Path) -> None:
    reviewer = (
        "---\nname: reviewer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "---\nbody\n"
    )
    wf = _make_workflow(
        tmp_path,
        agents={"stub.md": _OK_AGENT, "reviewer.md": reviewer},
        workflow_yaml=(
            "name: t\ndescription: \"\"\nphases: [p]\n"
            "p:\n  steps:\n"
            "    - name: s\n"
            "      file: agents/stub.md\n"
            "      gates:\n"
            "        - type: agent\n"
            "          file: agents/reviewer.md\n"
            "          model_role: missing-role\n"
            "          decide:\n"
            "            - {else: true, action: {kind: continue}}\n"
        ),
    )
    with pytest.raises(LoadErrors) as ei:
        load_workflow(wf, configured_roles=frozenset({"default"}))
    assert any("'missing-role'" in str(e) for e in ei.value.errors)


def test_v14_step_gate_agent_name_mismatch(tmp_path: Path) -> None:
    """An agent gate in a step referencing a misnamed agent file should fire V14."""
    mismatched = (
        "---\nname: not-reviewer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "---\nbody\n"
    )
    wf = _make_workflow(
        tmp_path,
        agents={"stub.md": _OK_AGENT, "reviewer.md": mismatched},
        workflow_yaml=(
            "name: t\ndescription: \"\"\nphases: [p]\n"
            "p:\n  steps:\n"
            "    - name: s\n"
            "      file: agents/stub.md\n"
            "      gates:\n"
            "        - type: agent\n"
            "          file: agents/reviewer.md\n"
            "          decide:\n"
            "            - {else: true, action: {kind: continue}}\n"
        ),
    )
    with pytest.raises(LoadErrors) as ei:
        load_workflow(wf)
    assert any("filename stem is 'reviewer'" in str(e) for e in ei.value.errors)

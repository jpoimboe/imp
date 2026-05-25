from pathlib import Path

import pytest

from imp.engine.errors import LoadError
from imp.engine.loader import parse_agent_file
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

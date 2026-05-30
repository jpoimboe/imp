"""Validator tests — new unified-step-model surface."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from imp.engine.errors import LoadError, LoadErrors
from imp.engine.loader import load_workflow


def _wf(tmp_path: Path, body: str, agents: dict[str, str] | None = None) -> Path:
    wf = tmp_path / "wf"
    (wf / "agents").mkdir(parents=True)
    (wf / "workflow.yaml").write_text(textwrap.dedent(body))
    for stem, b in (agents or {}).items():
        (wf / "agents" / f"{stem}.md").write_text(textwrap.dedent(b))
    return wf


# ── Reusable agent frontmatter snippets ──────────────────────────────────────

_OK_AGENT = """\
---
name: stub
tools: [complete_step]
output:
  status: {type: enum, values: [Success, Error]}
---
prompt body
"""

_WRITE_FILE_AGENT = """\
---
name: writer
tools: [write_file, complete_step]
output:
  status: {type: enum, values: [Success, Error]}
---
prompt body
"""

_READ_ONLY_AGENT = """\
---
name: reader
tools: [read_file, complete_step]
output:
  status: {type: enum, values: [Success, Error]}
---
prompt body
"""


# ── new cases from spec (Step 1) ─────────────────────────────────────────────


def test_goto_unknown_target_is_error(tmp_path: Path) -> None:
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: only
            steps:
              - name: b
                rules:
                  - {else: true, goto: nowhere}
        """)
    with pytest.raises((LoadError, LoadErrors)):
        load_workflow(wf)


def test_step_name_collides_with_phase_name_is_error(tmp_path: Path) -> None:
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: review
            steps:
              - name: review
                rules:
                  - {else: true, continue: true}
        """)
    with pytest.raises((LoadError, LoadErrors)):
        load_workflow(wf)


def test_duplicate_step_name_across_phases_is_error(tmp_path: Path) -> None:
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: a
            steps:
              - {name: dup, rules: [{else: true, continue: true}]}
          - name: b
            steps:
              - {name: dup, rules: [{else: true, continue: true}]}
        """)
    with pytest.raises((LoadError, LoadErrors)):
        load_workflow(wf)


def test_rules_without_else_is_error(tmp_path: Path) -> None:
    wf = _wf(tmp_path, """\
        name: t
        description: t
        vars: {x: {type: bool, default: false}}
        phases:
          - name: only
            steps:
              - name: b
                rules:
                  - {if: x, continue: true}
        """)
    with pytest.raises((LoadError, LoadErrors)):
        load_workflow(wf)


def test_non_trailing_empty_phase_is_error(tmp_path: Path) -> None:
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: a
            steps: []
          - name: b
            steps:
              - {name: s, rules: [{else: true, continue: true}]}
        """)
    with pytest.raises((LoadError, LoadErrors)):
        load_workflow(wf)


def test_inline_prompt_agent_is_rejected_this_milestone(tmp_path: Path) -> None:
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: only
            steps:
              - name: a
                prompt: "do the thing"
                rules:
                  - {else: true, continue: true}
        """)
    with pytest.raises((LoadError, LoadErrors)):
        load_workflow(wf)


# ── goto valid target resolves to phase or step name ─────────────────────────


def test_goto_phase_target_is_valid(tmp_path: Path) -> None:
    """goto: <phase-name> is valid in the shared namespace."""
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: loop
            steps:
              - name: b
                rules:
                  - {else: true, goto: loop}
        """)
    load_workflow(wf)  # must not raise


def test_goto_step_target_is_valid(tmp_path: Path) -> None:
    """goto: <step-name> is valid in the shared namespace."""
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: p
            steps:
              - name: first
                rules:
                  - {else: true, continue: true}
              - name: second
                rules:
                  - {else: true, goto: first}
        """)
    load_workflow(wf)  # must not raise


# ── _v_status_enum_present ────────────────────────────────────────────────────


def test_v2_status_missing(tmp_path: Path) -> None:
    bad_agent = """\
        ---
        name: bad
        tools: [complete_step]
        output:
          summary: {type: string}
        ---
        body
        """
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: p
            steps:
              - name: s
                file: agents/bad.md
        """, agents={"bad": textwrap.dedent(bad_agent)})
    with pytest.raises((LoadError, LoadErrors)) as ei:
        load_workflow(wf)
    assert any("must declare 'status' as enum" in str(e) for e in ei.value.errors)


# ── _v_agent_files_exist_and_named ───────────────────────────────────────────


def test_agent_file_missing_is_error(tmp_path: Path) -> None:
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: p
            steps:
              - name: s
                file: agents/missing.md
        """)
    with pytest.raises((LoadError, LoadErrors)) as ei:
        load_workflow(wf)
    assert any("agent file not found" in str(e) for e in ei.value.errors)


def test_v14_filename_must_match_name(tmp_path: Path) -> None:
    """agents/coder.md declaring name: not-coder raises an error."""
    mismatched = """\
        ---
        name: not-coder
        tools: [complete_step]
        output:
          status: {type: enum, values: [Success, Error]}
        ---
        body
        """
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: p
            steps:
              - name: s
                file: agents/coder.md
        """, agents={"coder": textwrap.dedent(mismatched)})
    with pytest.raises((LoadError, LoadErrors)) as ei:
        load_workflow(wf)
    assert any("filename stem is 'coder'" in str(e) for e in ei.value.errors)


# ── _v_fs_permissions_paths ───────────────────────────────────────────────────


def test_v11_fs_permissions_bad_paths(tmp_path: Path) -> None:
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: p
            steps:
              - name: s
                file: agents/stub.md
                fs_permissions:
                  - {operations: [read], paths: ['../escape'], mode: allow}
        """, agents={"stub": _OK_AGENT})
    with pytest.raises((LoadError, LoadErrors)) as ei:
        load_workflow(wf)
    assert any("'..'" in str(e) for e in ei.value.errors)


def test_v11_dotted_filename_allowed(tmp_path: Path) -> None:
    """Paths with '..' inside a filename component should NOT be flagged."""
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: p
            steps:
              - name: s
                file: agents/stub.md
                fs_permissions:
                  - {operations: [read], paths: ['/var/log/app..log'], mode: allow}
        """, agents={"stub": _OK_AGENT})
    load_workflow(wf)  # must not raise


# ── _v_model_role_configured ──────────────────────────────────────────────────


def test_v12_model_role_unconfigured(tmp_path: Path) -> None:
    """When configured_roles is supplied, an unconfigured role is an error."""
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: p
            steps:
              - name: s
                file: agents/stub.md
                model_role: missing-role
        """, agents={"stub": _OK_AGENT})
    with pytest.raises((LoadError, LoadErrors)) as ei:
        load_workflow(wf, configured_roles=frozenset({"default"}))
    assert any("'missing-role'" in str(e) for e in ei.value.errors)


# ── _v_write_tool_needs_write_permission ──────────────────────────────────────


def test_v15_pass_write_file_with_write_permission(tmp_path: Path) -> None:
    """A step declaring write_file AND an allow-write fs_permissions rule -> no error."""
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: p
            steps:
              - name: s
                file: agents/writer.md
                fs_permissions:
                  - {operations: [read], paths: ['/artifacts/**'], mode: allow}
                  - {operations: [write], paths: ['/artifacts/**'], mode: allow}
        """, agents={"writer": _WRITE_FILE_AGENT})
    load_workflow(wf)  # must not raise


def test_v15_fail_write_file_no_fs_permissions(tmp_path: Path) -> None:
    """Agent has write_file but step has NO fs_permissions -> error."""
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: p
            steps:
              - name: s
                file: agents/writer.md
        """, agents={"writer": _WRITE_FILE_AGENT})
    with pytest.raises((LoadError, LoadErrors)) as ei:
        load_workflow(wf)
    assert any(
        "write_file" in str(e) and "no fs_permissions" in str(e)
        for e in ei.value.errors
    )


def test_v15_fail_write_file_only_read_permission(tmp_path: Path) -> None:
    """Agent has write_file but fs_permissions only allows read -> error."""
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: p
            steps:
              - name: s
                file: agents/writer.md
                fs_permissions:
                  - {operations: [read], paths: ['/artifacts/**'], mode: allow}
        """, agents={"writer": _WRITE_FILE_AGENT})
    with pytest.raises((LoadError, LoadErrors)) as ei:
        load_workflow(wf)
    assert any(
        "write_file" in str(e) and "no fs_permissions" in str(e)
        for e in ei.value.errors
    )


def test_v15_pass_no_write_file_tool_empty_permissions(tmp_path: Path) -> None:
    """A step that does NOT declare write_file -> no error even with empty fs_permissions."""
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: p
            steps:
              - name: s
                file: agents/reader.md
        """, agents={"reader": _READ_ONLY_AGENT})
    load_workflow(wf)  # must not raise


# ── _v_required_blocks_per_kind ───────────────────────────────────────────────


def test_script_step_without_rules_is_error(tmp_path: Path) -> None:
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: p
            steps:
              - name: s
                command: "true"
        """)
    with pytest.raises((LoadError, LoadErrors)):
        load_workflow(wf)


def test_human_step_without_options_is_error(tmp_path: Path) -> None:
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: p
            steps:
              - name: h
                ask: "Proceed?"
        """)
    with pytest.raises((LoadError, LoadErrors)):
        load_workflow(wf)

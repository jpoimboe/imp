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

_GHOST_TIER_AGENT = """\
---
name: stub
tier: ghost-tier
tools: [complete_step]
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


def test_step_name_collides_with_phase_name_no_goto_is_allowed(tmp_path: Path) -> None:
    # A step sharing a name with its own phase is now allowed when no goto targets
    # the ambiguous name (resolve_goto prioritises step, so the collision is harmless).
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
    load_workflow(wf)  # must NOT raise


def test_step_name_collides_with_phase_name_with_goto_is_allowed(tmp_path: Path) -> None:
    # resolve_goto deterministically prefers steps over phases, so a goto to a name
    # that is both a step and phase is unambiguous — it targets the step. No error.
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: review
            steps:
              - name: review
                rules:
                  - {else: true, continue: true}
          - name: other
            steps:
              - name: doer
                rules:
                  - {else: true, goto: review}
        """)
    load_workflow(wf)  # must NOT raise


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


def test_read_only_dotted_filename_allowed(tmp_path: Path) -> None:
    load_workflow(_read_only_wf(tmp_path, "var/log/app..log"))  # should not raise


# ── _v_step_tier_configured ──────────────────────────────────────────────────


def test_v12_step_tier_unconfigured(tmp_path: Path) -> None:
    """When configured_tiers is supplied, an unconfigured tier is an error."""
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: p
            steps:
              - name: s
                file: agents/stub.md
                tier: missing-role
        """, agents={"stub": _OK_AGENT})
    with pytest.raises((LoadError, LoadErrors)) as ei:
        load_workflow(wf, configured_tiers=frozenset({"default"}))
    assert any("'missing-role'" in str(e) for e in ei.value.errors)


def test_v12_step_tier_unconfigured_from_agent_frontmatter(tmp_path: Path) -> None:
    """A step that omits tier: inherits the agent file's, and that inherited
    tier is validated against configured tiers at the step's own source."""
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: p
            steps:
              - name: s
                file: agents/stub.md
        """, agents={"stub": _GHOST_TIER_AGENT})
    step_source = load_workflow(wf).flat_steps()[0].source
    with pytest.raises(LoadErrors) as ei:
        load_workflow(wf, configured_tiers=frozenset({"medium"}))
    err = next(e for e in ei.value.errors if "ghost-tier" in str(e))
    assert err.source == step_source


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


# ── _v_resume_targets_are_agents ──────────────────────────────────────────────


def test_resume_to_agent_target_loads(tmp_path: Path) -> None:
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: a
            steps:
              - name: w
                file: agents/stub.md
              - name: gate
                file: agents/stub.md
                rules:
                  - {if: {status: Success}, resume: w}
                  - {else: true, continue: true}
        """, agents={"stub": _OK_AGENT})
    load_workflow(wf)   # no raise


def test_resume_to_script_target_is_rejected(tmp_path: Path) -> None:
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: a
            steps:
              - name: w
                file: agents/stub.md
              - name: chk
                command: ./x.sh
                rules:
                  - {if: {exit: 0}, continue: true}
                  - {else: true, resume: chk}
        """, agents={"stub": _OK_AGENT})
    with pytest.raises((LoadError, LoadErrors)) as ei:
        load_workflow(wf)
    assert any("resume" in str(e) and "agent" in str(e) for e in ei.value.errors)


def test_resume_to_empty_phase_is_rejected(tmp_path: Path) -> None:
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: a
            steps:
              - name: gate
                file: agents/stub.md
                rules:
                  - {else: true, resume: done}
          - name: done
            steps: []
        """, agents={"stub": _OK_AGENT})
    with pytest.raises((LoadError, LoadErrors)) as ei:
        load_workflow(wf)
    assert any("resume" in str(e) and "empty phase" in str(e) for e in ei.value.errors)


def test_resume_to_unknown_target_is_clean_load_error_not_keyerror(tmp_path: Path) -> None:
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: a
            steps:
              - name: gate
                file: agents/stub.md
                rules:
                  - {else: true, resume: ghost}
        """, agents={"stub": _OK_AGENT})
    # If the validator called resolve_goto without a namespace guard, this would
    # raise an uncaught KeyError instead of LoadErrors.
    with pytest.raises((LoadError, LoadErrors)) as ei:
        load_workflow(wf)
    assert any("ghost" in str(e) for e in ei.value.errors)


def test_v_step_tier_configured_flags_subagent_role() -> None:
    from imp.engine.types import (
        AgentDef,
        OutputField,
        OutputSchema,
        Pause,
        Phase,
        SourceLoc,
        Step,
        Vars,
        Workflow,
        WorkflowDefaults,
    )
    from imp.engine.validators import validate
    src = SourceLoc(file=Path("w.yaml"), line=1)

    def _agent(
        name: str, tier: str | None = None, subagents: tuple[str, ...] = ()
    ) -> AgentDef:
        return AgentDef(
            name=name, description="", tools=frozenset(),
            subagents=tuple(subagents),
            output_schema=OutputSchema(
                fields=(OutputField(name="status", type="enum",
                                    values=("Success", "Error")),), source=src),
            prompt_template_source="", source_path=Path(f"agents/{name}.md"),
            source=src, tier=tier)

    agents = {"coder": _agent("coder", subagents=("rev",)),
              "rev": _agent("rev", tier="ghost")}
    step = Step(name="coder", phase="p", kind="agent", rules=(),
                on_max_retries=Pause(), max_loops=6, source=src,
                file=Path("agents/coder.md"), tier="low")
    wf = Workflow(
        name="w", description="", phases=(Phase(name="p", steps=(step,),
                                                source=src),),
        vars=Vars(decls=()),
        defaults=WorkflowDefaults(max_retries=0, on_max_retries=Pause()),
        agents=agents, workflow_dir=Path("."), source=src)
    errors, _ = validate(wf, configured_tiers=frozenset({"default"}))
    assert any("ghost" in e.message for e in errors)


# ── _v_subagent_forbidden_tools ───────────────────────────────────────────────

def test_top_level_rename_quest_agent_not_dispatched_is_allowed(tmp_path: Path) -> None:
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: p
            steps:
              - name: s
                file: agents/renamer.md
        """, agents={"renamer": _RENAMER_SUBAGENT})
    load_workflow(wf)  # must not raise


def test_subagent_declaring_wait_for_user_no_longer_rejected(tmp_path: Path) -> None:
    # wait_for_user is no longer in _SUBAGENT_FORBIDDEN_TOOLS, so a dispatchable
    # subagent that declares it loads without error.
    sub = (
        "---\nname: waiter\ntools: [wait_for_user, complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\nbody\n"
    )
    lead = (
        "---\nname: lead\ntools: [sub_agent, complete_step]\nsubagents: [waiter]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\nbody\n"
    )
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: p
            steps:
              - name: s
                file: agents/lead.md
        """, agents={"lead": lead, "waiter": sub})
    load_workflow(wf)  # must not raise


def test_dual_role_agent_as_step_and_subagent_with_rename_quest_is_error(tmp_path: Path) -> None:
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: p
            steps:
              - name: s1
                file: agents/renamer.md
              - name: s2
                file: agents/lead.md
        """, agents={"lead": _LEAD_WITH_RENAMER_SUB, "renamer": _RENAMER_SUBAGENT})
    with pytest.raises((LoadError, LoadErrors)) as ei:
        load_workflow(wf)
    assert any(
        "renamer" in str(e) and "rename_quest" in str(e)
        for e in ei.value.errors
    )


def test_resume_in_defaults_on_max_retries_to_non_agent_is_rejected(tmp_path: Path) -> None:
    wf = _wf(tmp_path, """\
        name: t
        description: t
        defaults:
          on_max_retries: {resume: chk}
        phases:
          - name: a
            steps:
              - name: gate
                file: agents/stub.md
                rules:
                  - {if: {status: Success}, continue: true}
                  - {else: true, pause: true}
              - name: chk
                command: ./x.sh
                rules:
                  - {else: true, continue: true}
        """, agents={"stub": _OK_AGENT})
    with pytest.raises((LoadError, LoadErrors)) as ei:
        load_workflow(wf)
    assert any("resume" in str(e) and "agent" in str(e) for e in ei.value.errors)


_TASKER_SUBAGENT = """\
---
name: tasker
tools: [sub_agent, complete_step]
output:
  status: {type: enum, values: [Success, Error]}
---
prompt body
"""

_RENAMER_SUBAGENT = """\
---
name: renamer
tools: [rename_quest, complete_step]
output:
  status: {type: enum, values: [Success, Error]}
---
prompt body
"""

_LEAD_WITH_TASKER_SUB = """\
---
name: lead
tools: [sub_agent, complete_step]
subagents: [tasker]
output:
  status: {type: enum, values: [Success, Error]}
---
prompt body
"""

_LEAD_WITH_RENAMER_SUB = """\
---
name: lead
tools: [sub_agent, complete_step]
subagents: [renamer]
output:
  status: {type: enum, values: [Success, Error]}
---
prompt body
"""

_PLAIN_SUBAGENT = """\
---
name: plain
output:
  status: {type: enum, values: [Success, Error]}
---
prompt body
"""

_LEAD_WITH_PLAIN_SUB = """\
---
name: lead
subagents: [plain]
output:
  status: {type: enum, values: [Success, Error]}
---
prompt body
"""


def test_dispatchable_subagent_with_task_is_error(tmp_path: Path) -> None:
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: p
            steps:
              - name: s
                file: agents/lead.md
        """, agents={"lead": _LEAD_WITH_TASKER_SUB, "tasker": _TASKER_SUBAGENT})
    with pytest.raises((LoadError, LoadErrors)) as ei:
        load_workflow(wf)
    assert any("tasker" in str(e) and "sub_agent" in str(e) for e in ei.value.errors)


def test_dispatchable_subagent_with_rename_quest_is_error(tmp_path: Path) -> None:
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: p
            steps:
              - name: s
                file: agents/lead.md
        """, agents={"lead": _LEAD_WITH_RENAMER_SUB, "renamer": _RENAMER_SUBAGENT})
    with pytest.raises((LoadError, LoadErrors)) as ei:
        load_workflow(wf)
    assert any(
        "renamer" in str(e) and "rename_quest" in str(e) for e in ei.value.errors)


def test_default_resolved_subagent_has_no_forbidden_tools(tmp_path: Path) -> None:
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: p
            steps:
              - name: s
                file: agents/lead.md
        """, agents={"lead": _LEAD_WITH_PLAIN_SUB, "plain": _PLAIN_SUBAGENT})
    load_workflow(wf)  # must not raise — `plain` resolves to BASE_CAPS only


# ── agent template syntax ────────────────────────────────────────────────────

def _agent_with_body(body: str) -> str:
    return (
        "---\nname: stub\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\n"
        + body + "\n"
    )


_STUB_WF = """\
name: t
description: t
phases:
  - name: p
    steps:
      - name: s
        file: agents/stub.md
"""


def test_jinja_syntax_error_is_loaderror(tmp_path: Path) -> None:
    wf = _wf(tmp_path, _STUB_WF,
             agents={"stub": _agent_with_body("{{ vars.foo ")})
    with pytest.raises((LoadError, LoadErrors)) as ei:
        load_workflow(wf)
    assert "template syntax error" in "\n".join(str(e) for e in ei.value.errors)


def test_project_reference_is_not_validated_at_load(tmp_path: Path) -> None:
    # No facts machinery: `project` is an ordinary undefined name, caught at
    # render, not statically at load.
    wf = _wf(tmp_path, _STUB_WF,
             agents={"stub": _agent_with_body("{{ project.anything }}")})
    load_workflow(wf)  # must not raise


# ── _v_read_only_paths ────────────────────────────────────────────────────────

def _read_only_wf(tmp_path: Path, path_literal: str):
    return _wf(tmp_path, f"""\
        name: t
        description: t
        phases:
          - name: p
            steps:
              - name: s
                file: agents/stub.md
                read_only: ['{path_literal}']
        """, agents={"stub": _OK_AGENT})


def test_read_only_parses_into_step(tmp_path: Path) -> None:
    wf = load_workflow(_read_only_wf(tmp_path, "src/vendor"))
    step = next(s for s in wf.flat_steps() if s.name == "s")
    assert step.read_only == ("src/vendor",)


def test_read_only_rejects_glob_metachar(tmp_path: Path) -> None:
    with pytest.raises((LoadError, LoadErrors)) as ei:
        load_workflow(_read_only_wf(tmp_path, "src/**"))
    assert any("glob" in str(e) for e in ei.value.errors)


def test_read_only_rejects_parent_segment(tmp_path: Path) -> None:
    with pytest.raises((LoadError, LoadErrors)) as ei:
        load_workflow(_read_only_wf(tmp_path, "../escape"))
    assert any("'..'" in str(e) for e in ei.value.errors)


def test_read_only_rejects_absolute_path(tmp_path: Path) -> None:
    with pytest.raises((LoadError, LoadErrors)) as ei:
        load_workflow(_read_only_wf(tmp_path, "/etc"))
    assert any("leading '/'" in str(e) for e in ei.value.errors)


def test_read_only_rejects_home_path(tmp_path: Path) -> None:
    with pytest.raises((LoadError, LoadErrors)) as ei:
        load_workflow(_read_only_wf(tmp_path, "~/secrets"))
    assert any("'~'" in str(e) for e in ei.value.errors)


def test_v2_status_not_required_for_dispatched_subagent(tmp_path: Path) -> None:
    # A dispatched subagent routes on no typed field, so it needs no status enum.
    lead = """\
        ---
        name: lead
        subagents: [helper]
        output:
          status: {type: enum, values: [Success, Error]}
        ---
        body
        """
    helper = """\
        ---
        name: helper
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
                file: agents/lead.md
        """, agents={"lead": textwrap.dedent(lead),
                     "helper": textwrap.dedent(helper)})
    load_workflow(wf)  # must not raise: helper is a subagent, not a step


def test_read_only_on_non_agent_step_is_silently_ignored(tmp_path: Path) -> None:
    wf = _wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: p
            steps:
              - name: s
                command: echo hi
                read_only: ['.']
                rules:
                  - {else: true, continue: true}
        """)
    loaded = load_workflow(wf)
    step = next(s for s in loaded.flat_steps() if s.name == "s")
    assert step.read_only == ()


def test_unknown_tool_grants_warn_not_fatal(tmp_path: Path) -> None:
    """AC 3(a,b) + exact warning copy: an unknown deny_tools/allow_tools entry no longer raises;
    it loads, is ignored (effective tools unchanged), and each offending entry
    lands on Workflow.load_warnings with the exact skew copy."""
    wf = _wf(
        tmp_path,
        """\
        name: t
        description: t
        defaults: {max_retries: 1, max_loops: 6}
        phases:
          - name: only
            steps:
              - {name: w, file: agents/stub.md}
        """,
        agents={
            "stub": """\
            ---
            name: stub
            deny_tools: [bogus_deny]
            allow_tools: [bogus_allow]
            output:
              status: {type: enum, values: [Success, Error]}
            ---
            prompt body
            """
        },
    )

    loaded = load_workflow(wf)  # must NOT raise

    assert loaded.load_warnings == (
        "stub: deny_tools lists 'bogus_deny', unknown to this engine "
        "— ignoring (workflow/engine version skew?)",
        "stub: allow_tools lists 'bogus_allow', unknown to this engine "
        "— ignoring (workflow/engine version skew?)",
    )
    agent = loaded.agents["stub"]
    assert "bogus_deny" not in agent.tools
    assert "bogus_allow" not in agent.tools

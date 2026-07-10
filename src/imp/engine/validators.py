"""All workflow validators. Accumulate errors and warnings; caller decides whether to raise."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from imp.engine.errors import LoadError
from imp.engine.types import (
    AgentDef,
    Goto,
    SourceLoc,
    Workflow,
)


def validate(
    workflow: Workflow,
    *,
    configured_tiers: frozenset[str] | None = None,
    project_keys: frozenset[str] = frozenset(),
) -> tuple[list[LoadError], list[LoadError]]:
    """Run every validator. Returns (errors, warnings).

    `configured_tiers` enables _v_step_tier_configured / subagent tier checks
    when supplied (the change-creation backstop; resolve_run is authoritative).
    """
    errors: list[LoadError] = []
    warnings: list[LoadError] = []

    _v_agent_files_exist_and_named(workflow, errors)
    _v_status_enum_present(workflow, errors)
    _v_goto_targets_in_shared_namespace(workflow, errors)
    _v_resume_targets_are_agents(workflow, errors)
    _v_unique_step_names_disjoint_from_phases(workflow, errors)
    _v_rules_end_in_else(workflow, errors)
    _v_required_blocks_per_kind(workflow, errors)
    _v_empty_phase_is_last(workflow, errors)
    _v_agent_has_file(workflow, errors)
    _v_read_only_paths(workflow, errors)
    if configured_tiers is not None:
        _v_step_tier_configured(workflow, configured_tiers, errors)
    _v_differ_from_targets_exist(workflow, errors)
    _v_subagent_forbidden_tools(workflow, errors)
    _v_project_refs(workflow, project_keys, errors)

    return errors, warnings


def _v_agent_files_exist_and_named(workflow: Workflow, errors: list[LoadError]) -> None:
    """Each agent step's file: must exist on disk and file.stem must equal agent name."""
    by_path: dict[Path, AgentDef] = {
        a.source_path: a for a in workflow.agents.values()
    }
    for step in workflow.flat_steps():
        if step.kind != "agent" or step.file is None:
            continue
        if not step.file.exists():
            errors.append(LoadError(
                f"step {step.name!r}: agent file not found: {step.file}",
                step.source,
            ))
        else:
            agent = by_path.get(step.file)
            if agent is not None and step.file.stem != agent.name:
                errors.append(LoadError(
                    f"agent file {step.file.name}: declares 'name: {agent.name}' "
                    f"but the filename stem is {step.file.stem!r} (they must match)",
                    step.source,
                ))


def _v_status_enum_present(workflow: Workflow, errors: list[LoadError]) -> None:
    # Status is a step's routing signal; a dispatched subagent routes on no field,
    # so require it only for step-agents.
    step_agent_names = {
        s.file.stem for s in workflow.flat_steps()
        if s.kind == "agent" and s.file is not None
    }
    for agent in workflow.agents.values():
        if agent.name not in step_agent_names:
            continue
        sf = agent.output_schema.status_field()
        if sf is None or sf.type != "enum":
            errors.append(
                LoadError(
                    f"agent {agent.name!r}: output schema must declare 'status' as enum",
                    agent.source,
                )
            )
            continue
        missing = {"Success", "Error"} - set(sf.values)
        if missing:
            errors.append(
                LoadError(
                    f"agent {agent.name!r}: status enum missing {sorted(missing)}",
                    agent.source,
                )
            )


def _v_goto_targets_in_shared_namespace(
    workflow: Workflow, errors: list[LoadError]
) -> None:
    names = {p.name for p in workflow.phases} | {s.name for s in workflow.flat_steps()}

    def check(action: object, source: object) -> None:
        if isinstance(action, Goto) and action.target not in names:
            errors.append(LoadError(
                f"goto target {action.target!r} is not a phase or step "
                f"{sorted(names)}",
                source,  # type: ignore[arg-type]
            ))

    for step in workflow.flat_steps():
        for rule in step.rules:
            check(rule.action, rule.source)
    check(workflow.defaults.on_max_retries, workflow.source)


def _v_resume_targets_are_agents(
    workflow: Workflow, errors: list[LoadError]
) -> None:
    """A `resume:` edge (Goto with resume=True) must resolve to an agent step.
    Phase targets resolve to the phase's first step; an empty (trailing) phase
    has no step to resume and is rejected. Unknown targets are skipped here —
    _v_goto_targets_in_shared_namespace reports those (and resolve_goto would
    otherwise raise an uncaught KeyError, since validators do not short-circuit).
    """
    flat = workflow.flat_steps()
    names = {p.name for p in workflow.phases} | {s.name for s in flat}

    def check(action: object, source: object) -> None:
        if not (isinstance(action, Goto) and action.resume):
            return
        if action.target not in names:
            return  # reported by _v_goto_targets_in_shared_namespace
        idx = workflow.resolve_goto(action.target)
        if idx >= len(flat):
            errors.append(LoadError(
                f"resume target {action.target!r} is an empty phase with no "
                f"step to resume",
                source,  # type: ignore[arg-type]
            ))
            return
        if flat[idx].kind != "agent":
            errors.append(LoadError(
                f"resume target {action.target!r} resolves to a "
                f"{flat[idx].kind!r} step; resume: requires an agent step",
                source,  # type: ignore[arg-type]
            ))

    for step in flat:
        for rule in step.rules:
            check(rule.action, rule.source)
    check(workflow.defaults.on_max_retries, workflow.source)


def _v_unique_step_names_disjoint_from_phases(
    workflow: Workflow, errors: list[LoadError]
) -> None:
    # resolve_goto prioritises steps over phases, so a step sharing a phase name
    # is always resolved deterministically — no collision check needed.
    flat = workflow.flat_steps()
    seen: dict[str, object] = {}
    for step in flat:
        if step.name in seen:
            errors.append(LoadError(
                f"duplicate step name {step.name!r} (names are global)", step.source))
        seen[step.name] = step


def _v_rules_end_in_else(workflow: Workflow, errors: list[LoadError]) -> None:
    for step in workflow.flat_steps():
        if step.rules and not step.rules[-1].is_else:
            errors.append(LoadError(
                f"rules: in step {step.name!r} must end with `else: true`",
                step.rules[-1].source))


def _v_required_blocks_per_kind(workflow: Workflow, errors: list[LoadError]) -> None:
    for step in workflow.flat_steps():
        if step.kind in ("script", "branch", "merge_and_exit_worktree") \
                and not step.rules:
            errors.append(LoadError(
                f"{step.kind} step {step.name!r} requires a rules: block", step.source))


def _v_empty_phase_is_last(workflow: Workflow, errors: list[LoadError]) -> None:
    for i, p in enumerate(workflow.phases):
        if not p.steps and i != len(workflow.phases) - 1:
            errors.append(LoadError(
                f"empty phase {p.name!r} must be the last phase "
                "(a non-trailing empty phase would silently terminate the run)",
                p.source))


def _v_agent_has_file(workflow: Workflow, errors: list[LoadError]) -> None:
    """Inline prompt: agents are deferred this milestone -- an agent step needs a
    file:. Without this, the executor's workflow.agents[step.file.stem] would
    crash on a fileless agent. Lift this check when prompt: lands."""
    for step in workflow.flat_steps():
        if step.kind == "agent" and step.file is None:
            errors.append(LoadError(
                f"agent step {step.name!r} uses inline prompt:, which is not "
                "supported yet -- use file: agents/<name>.md", step.source))


def _v_read_only_paths(workflow: Workflow, errors: list[LoadError]) -> None:
    for step in workflow.flat_steps():
        if step.kind != "agent":
            continue
        for p in step.read_only:
            if p.startswith("/"):
                errors.append(LoadError(
                    f"read_only path must be worktree-relative (no leading '/'): {p!r}",
                    step.source))
            if any(part == ".." for part in PurePosixPath(p).parts):
                errors.append(LoadError(
                    f"read_only path may not contain '..' segment: {p!r}", step.source))
            if p.startswith("~"):
                errors.append(LoadError(
                    f"read_only path may not start with '~': {p!r}", step.source))
            if any(ch in p for ch in "*?[]"):
                errors.append(LoadError(
                    f"read_only path may not contain glob metacharacters "
                    f"(binds are path-based): {p!r}", step.source))


def _v_step_tier_configured(
    workflow: Workflow, configured: frozenset[str], errors: list[LoadError],
) -> None:
    """Every agent step's `tier:` (and every subagent frontmatter `tier:`
    reachable one level deep) must be a configured tier name."""
    seen: set[tuple[str, str]] = set()

    def check(tier: str, source: SourceLoc | None) -> None:
        if tier in configured:
            return
        key = (tier, str(source))
        if key in seen:
            return
        seen.add(key)
        errors.append(
            LoadError(
                f"tier {tier!r} is not a configured tier",
                source,
                hint=f"configured tiers: {sorted(configured)}",
            )
        )

    for step in workflow.flat_steps():
        if step.kind != "agent":
            continue
        check(step.tier, step.source)
        if step.file is not None:
            agent = workflow.agents.get(step.file.stem)
            if agent is not None:
                for sub_name in agent.subagents:
                    sub = workflow.agents.get(sub_name)
                    if sub is not None and sub.tier is not None:
                        check(sub.tier, sub.source)


def _v_differ_from_targets_exist(
    workflow: Workflow, errors: list[LoadError],
) -> None:
    """Every step.differ_from must name an agent step in the same workflow.
    (Cycle detection runs in loader.load_workflow.)"""
    step_names = {s.name for s in workflow.flat_steps()
                  if s.kind == "agent"}
    for step in workflow.flat_steps():
        if step.kind != "agent" or step.differ_from is None:
            continue
        target = step.differ_from
        if target == step.name:
            errors.append(
                LoadError(
                    f"differ_from on step {step.name!r} cannot reference itself",
                    step.source,
                )
            )
            continue
        if target not in step_names:
            errors.append(
                LoadError(
                    f"differ_from target {target!r} (on step {step.name!r}) "
                    f"does not name an agent step in this workflow",
                    step.source,
                )
            )


_SUBAGENT_FORBIDDEN_TOOLS = ("sub_agent", "rename_quest")


def _v_subagent_forbidden_tools(
    workflow: Workflow, errors: list[LoadError]
) -> None:
    dispatchable: set[str] = set()
    for agent in workflow.agents.values():
        dispatchable.update(agent.subagents)
    for name in sorted(dispatchable):
        adef = workflow.agents.get(name)
        if adef is None:
            continue  # unknown-subagent reference reported elsewhere
        for tool in _SUBAGENT_FORBIDDEN_TOOLS:
            if tool in adef.tools:
                errors.append(
                    LoadError(
                        f"subagent {name!r} is parallel-dispatchable (named in a "
                        f"`subagents:` list) and declares {tool!r}; a dispatched "
                        f"subagent cannot dispatch or rename the change",
                        adef.source,
                        hint=f"remove {tool!r} from the subagent's tools",
                    )
                )


def _v_project_refs(
    workflow: Workflow, project_keys: frozenset[str], errors: list[LoadError],
) -> None:
    import jinja2
    from jinja2 import nodes

    env = jinja2.Environment()
    known = set(project_keys) | {"guidance"}
    for agent in workflow.agents.values():
        try:
            ast = env.parse(agent.prompt_template_source)
        except jinja2.exceptions.TemplateSyntaxError as e:
            errors.append(LoadError(
                f"agent {agent.name!r}: template syntax error: {e.message}",
                agent.source,
            ))
            continue
        for ga in ast.find_all(nodes.Getattr):
            if not (isinstance(ga.node, nodes.Name) and ga.node.name == "project"):
                continue
            key = ga.attr
            if key not in known:
                errors.append(LoadError(
                    f"agent {agent.name!r}: unknown project fact {key!r} "
                    f"in '{{{{ project.{key} }}}}'",
                    agent.source,
                    hint=f"configured project keys: {sorted(known)}",
                ))
        for gi in ast.find_all(nodes.Getitem):
            if not (isinstance(gi.node, nodes.Name) and gi.node.name == "project"):
                continue
            if not (isinstance(gi.arg, nodes.Const) and isinstance(gi.arg.value, str)):
                continue  # dynamic subscript: checked at render, not statically
            key = gi.arg.value
            if key not in known:
                errors.append(LoadError(
                    f"agent {agent.name!r}: unknown project fact {key!r} "
                    f"in '{{{{ project[\"{key}\"] }}}}'",
                    agent.source,
                    hint=f"configured project keys: {sorted(known)}",
                ))

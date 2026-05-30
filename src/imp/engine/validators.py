"""All workflow validators. Accumulate errors and warnings; caller decides whether to raise."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from imp.engine.errors import LoadError
from imp.engine.types import (
    AgentDef,
    FsRule,
    Goto,
    Workflow,
)


def validate(
    workflow: Workflow, *, configured_roles: frozenset[str] | None = None
) -> tuple[list[LoadError], list[LoadError]]:
    """Run every validator. Returns (errors, warnings).

    `configured_roles` enables _v_model_role_configured when supplied.
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
    _v_fs_permissions_paths(workflow, errors)
    if configured_roles is not None:
        _v_model_role_configured(workflow, configured_roles, errors)
    _v_write_tool_needs_write_permission(workflow, errors)

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
    for agent in workflow.agents.values():
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
        for opt in step.options:
            check(opt.action, opt.source)
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
        for opt in step.options:
            check(opt.action, opt.source)
    check(workflow.defaults.on_max_retries, workflow.source)


def _v_unique_step_names_disjoint_from_phases(
    workflow: Workflow, errors: list[LoadError]
) -> None:
    phase_names = {p.name for p in workflow.phases}
    seen: dict[str, object] = {}
    for step in workflow.flat_steps():
        if step.name in phase_names:
            errors.append(LoadError(
                f"step {step.name!r} collides with a phase name", step.source))
        if step.name in seen:
            errors.append(LoadError(
                f"duplicate step name {step.name!r} (names are global)", step.source))
        seen[step.name] = step


def _v_rules_end_in_else(workflow: Workflow, errors: list[LoadError]) -> None:
    for step in workflow.flat_steps():
        if step.kind == "human":
            continue
        if step.rules and not step.rules[-1].is_else:
            errors.append(LoadError(
                f"rules: in step {step.name!r} must end with `else: true`",
                step.rules[-1].source))


def _v_required_blocks_per_kind(workflow: Workflow, errors: list[LoadError]) -> None:
    for step in workflow.flat_steps():
        if step.kind in ("script", "branch") and not step.rules:
            errors.append(LoadError(
                f"{step.kind} step {step.name!r} requires a rules: block", step.source))
        if step.kind == "human" and not step.options:
            errors.append(LoadError(
                f"human step {step.name!r} requires options:", step.source))


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


def _v_fs_permissions_paths(workflow: Workflow, errors: list[LoadError]) -> None:
    for step in workflow.flat_steps():
        if step.kind != "agent":
            continue
        for rule in step.fs_permissions:
            _v_check_paths(rule, errors)


def _v_check_paths(rule: FsRule, errors: list[LoadError]) -> None:
    for p in rule.paths:
        if not p.startswith("/"):
            errors.append(
                LoadError(f"fs_permissions path must start with '/': {p!r}", rule.source)
            )
        # Use path components so e.g. "/var/log/app..log" is not flagged.
        parts = PurePosixPath(p).parts
        if any(part == ".." for part in parts):
            errors.append(
                LoadError(
                    f"fs_permissions path may not contain '..' segment: {p!r}",
                    rule.source,
                )
            )
        if p.startswith("~"):
            errors.append(
                LoadError(
                    f"fs_permissions path may not start with '~': {p!r}",
                    rule.source,
                )
            )


def _v_model_role_configured(
    workflow: Workflow, configured: frozenset[str], errors: list[LoadError]
) -> None:
    seen: set[tuple[str, str]] = set()

    def check(role: str, source: object) -> None:
        if role in configured:
            return
        key = (role, str(source))
        if key in seen:
            return
        seen.add(key)
        errors.append(
            LoadError(
                f"model_role {role!r} is not configured in .imp/config.yaml",
                source,  # type: ignore[arg-type]
                hint=f"configured roles: {sorted(configured)}",
            )
        )

    for step in workflow.flat_steps():
        if step.kind == "agent":
            check(step.model_role, step.source)


def _v_write_tool_needs_write_permission(
    workflow: Workflow, errors: list[LoadError]
) -> None:
    """A step whose agent declares `write_file` in its tools must have at least
    one fs_permissions rule with mode=='allow' and 'write' in operations.
    Catches the Bug 2 class: agent can call write_file but the step grants no
    write path, so every write silently fails with PermissionDenied.
    """

    def _has_write_allow(fs_permissions: tuple[FsRule, ...]) -> bool:
        return any(
            rule.mode == "allow" and "write" in rule.operations
            for rule in fs_permissions
        )

    for step in workflow.flat_steps():
        if step.kind != "agent" or step.file is None:
            continue
        agent = workflow.agents.get(step.file.stem)
        if agent is None:
            continue  # _v_agent_files_exist_and_named reports the missing-file case
        if "write_file" not in agent.tools:
            continue
        if not _has_write_allow(step.fs_permissions):
            errors.append(
                LoadError(
                    f"agent {agent.name!r} declares tool 'write_file' but the "
                    f"step/gate has no fs_permissions rule with "
                    f"mode=allow and operation=write",
                    step.source,
                )
            )

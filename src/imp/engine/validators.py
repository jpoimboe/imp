"""All workflow validators. Accumulate errors and warnings; caller decides whether to raise."""

from __future__ import annotations

from pathlib import Path

from imp.engine.errors import LoadError
from imp.engine.types import (
    AgentDef,
    AgentGate,
    Decide,
    FsRule,
    Goto,
    HumanGate,
    Retry,
    ScriptGate,
    Step,
    Workflow,
)


def validate(
    workflow: Workflow, *, configured_roles: frozenset[str] | None = None
) -> tuple[list[LoadError], list[LoadError]]:
    """Run every validator. Returns (errors, warnings).

    `configured_roles` enables V12 when supplied. Task 13 starts threading
    `load_config(project_root).role_names()`; Tasks 14 + 21 plumb it through
    Change.create and run.
    """
    errors: list[LoadError] = []
    warnings: list[LoadError] = []

    _v1_step_files_exist_and_parsed(workflow, errors)
    _v2_status_enum_present(workflow, errors)
    _v3_complete_step_in_tools(workflow, errors)
    _v4_decide_when_keys_match_schema(workflow, errors)
    _v5_goto_targets_valid(workflow, errors)
    _v7_human_gate_options(workflow, errors)
    _v8_unique_step_names(workflow, errors)
    _v9_on_max_retries_not_retry(workflow, errors)
    _v11_fs_permissions_paths(workflow, errors)
    if configured_roles is not None:
        _v12_model_role_configured(workflow, configured_roles, errors)
    _v13_decide_missing_else(workflow, warnings)
    _v14_filename_matches_agent_name(workflow, errors)

    return errors, warnings


def _v1_step_files_exist_and_parsed(workflow: Workflow, errors: list[LoadError]) -> None:
    """Each step's `file:` must exist on disk. (Filename-name matching is V14.)"""
    for phase in workflow.phases:
        for step in phase.steps:
            if not step.file.exists():
                errors.append(
                    LoadError(
                        f"step {step.name!r}: agent file not found: {step.file}",
                        step.source,
                    )
                )
        for gate in phase.gates:
            if isinstance(gate, AgentGate) and not gate.file.exists():
                errors.append(
                    LoadError(
                        f"agent gate file not found: {gate.file}",
                        gate.source,
                    )
                )


def _v2_status_enum_present(workflow: Workflow, errors: list[LoadError]) -> None:
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


def _v3_complete_step_in_tools(workflow: Workflow, errors: list[LoadError]) -> None:
    for agent in workflow.agents.values():
        if "complete_step" not in agent.tools:
            errors.append(
                LoadError(
                    f"agent {agent.name!r}: 'complete_step' must be in tools:",
                    agent.source,
                )
            )


def _v4_decide_when_keys_match_schema(
    workflow: Workflow, errors: list[LoadError]
) -> None:
    SCRIPT_FIELDS = frozenset({"exit", "stdout", "stderr"})
    for phase in workflow.phases:
        for step in phase.steps:
            for gate in step.gates:
                _v4_check_gate(workflow, gate, SCRIPT_FIELDS, errors)
        for gate in phase.gates:
            _v4_check_gate(workflow, gate, SCRIPT_FIELDS, errors)


def _v4_check_gate(
    workflow: Workflow, gate: object, script_fields: frozenset[str],
    errors: list[LoadError],
) -> None:
    if isinstance(gate, ScriptGate):
        for rule in gate.decide.rules:
            for k, _ in rule.when:
                if k not in script_fields:
                    errors.append(
                        LoadError(
                            f"script gate decide: 'when.{k}' not a script field "
                            f"(allowed: {sorted(script_fields)})",
                            rule.source,
                        )
                    )
    elif isinstance(gate, AgentGate):
        agent = workflow.agents.get(gate.file.stem)
        if agent is None:
            return
        allowed = agent.output_schema.field_names()
        for rule in gate.decide.rules:
            for k, _ in rule.when:
                if k not in allowed:
                    errors.append(
                        LoadError(
                            f"agent gate decide: 'when.{k}' not in "
                            f"{agent.name!r}'s output schema {sorted(allowed)}",
                            rule.source,
                        )
                    )


def _v5_goto_targets_valid(workflow: Workflow, errors: list[LoadError]) -> None:
    valid_phases = {p.name for p in workflow.phases}

    def check_action(action: object, source: object) -> None:
        if isinstance(action, Goto) and action.target not in valid_phases:
            errors.append(
                LoadError(
                    f"goto target {action.target!r} is not in phases: "
                    f"{sorted(valid_phases)}",
                    source,  # type: ignore[arg-type]
                )
            )

    check_action(workflow.defaults.on_max_retries, workflow.source)
    for phase in workflow.phases:
        check_action(phase.on_max_retries, phase.source)
        for step in phase.steps:
            check_action(step.on_max_retries, step.source)
            for gate in step.gates:
                _v5_walk_gate(gate, check_action)
        for gate in phase.gates:
            _v5_walk_gate(gate, check_action)


def _v5_walk_gate(gate: object, check):  # type: ignore[no-untyped-def]
    if isinstance(gate, HumanGate):
        for opt in gate.options:
            check(opt.action, opt.source)
    elif isinstance(gate, (ScriptGate, AgentGate)):
        for rule in gate.decide.rules:
            check(rule.action, rule.source)


def _v7_human_gate_options(workflow: Workflow, errors: list[LoadError]) -> None:
    for phase in workflow.phases:
        for gate in (*[g for s in phase.steps for g in s.gates], *phase.gates):
            if isinstance(gate, HumanGate) and not gate.options:
                errors.append(
                    LoadError("human gate must have at least one option", gate.source)
                )


def _v8_unique_step_names(workflow: Workflow, errors: list[LoadError]) -> None:
    for phase in workflow.phases:
        seen: dict[str, Step] = {}
        for step in phase.steps:
            if step.name in seen:
                errors.append(
                    LoadError(
                        f"duplicate step name {step.name!r} in phase {phase.name!r}",
                        step.source,
                    )
                )
            seen[step.name] = step


def _v9_on_max_retries_not_retry(workflow: Workflow, errors: list[LoadError]) -> None:
    def check(action: object, source: object) -> None:
        if isinstance(action, Retry):
            errors.append(
                LoadError(
                    "on_max_retries cannot itself be 'retry' "
                    "(would loop infinitely)",
                    source,  # type: ignore[arg-type]
                )
            )

    check(workflow.defaults.on_max_retries, workflow.source)
    for phase in workflow.phases:
        check(phase.on_max_retries, phase.source)
        for step in phase.steps:
            check(step.on_max_retries, step.source)


def _v11_fs_permissions_paths(workflow: Workflow, errors: list[LoadError]) -> None:
    for phase in workflow.phases:
        for step in phase.steps:
            for rule in step.fs_permissions:
                _v11_check_paths(rule, errors)
        for gate in phase.gates:
            if isinstance(gate, AgentGate):
                for rule in gate.fs_permissions:
                    _v11_check_paths(rule, errors)


def _v11_check_paths(rule: FsRule, errors: list[LoadError]) -> None:
    for p in rule.paths:
        if not p.startswith("/"):
            errors.append(
                LoadError(f"fs_permissions path must start with '/': {p!r}", rule.source)
            )
        if ".." in p or p.startswith("~"):
            errors.append(
                LoadError(
                    f"fs_permissions path may not contain '..' or '~': {p!r}",
                    rule.source,
                )
            )


def _v12_model_role_configured(
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

    for phase in workflow.phases:
        for step in phase.steps:
            check(step.model_role, step.source)
        for gate in phase.gates:
            if isinstance(gate, AgentGate):
                check(gate.model_role, gate.source)


def _v13_decide_missing_else(workflow: Workflow, warnings: list[LoadError]) -> None:
    def check(decide: Decide, where: str) -> None:
        if not any(r.is_else for r in decide.rules):
            warnings.append(
                LoadError(
                    f"decide block in {where} has no terminal 'else: true' rule "
                    "(fall-through is undefined)",
                    decide.source,
                )
            )

    for phase in workflow.phases:
        for step in phase.steps:
            for gate in step.gates:
                if isinstance(gate, (ScriptGate, AgentGate)):
                    check(gate.decide, f"step {step.name!r}")
        for gate in phase.gates:
            if isinstance(gate, (ScriptGate, AgentGate)):
                check(gate.decide, f"phase {phase.name!r}")


def _v14_filename_matches_agent_name(
    workflow: Workflow, errors: list[LoadError]
) -> None:
    """Each `agents/<stem>.md` must declare `name: <stem>` in its frontmatter."""
    by_path: dict[Path, AgentDef] = {
        a.source_path: a for a in workflow.agents.values()
    }

    def check(file: Path, source: object) -> None:
        agent = by_path.get(file)
        if agent is None:
            return  # V1 reports the missing-file case
        if file.stem != agent.name:
            errors.append(
                LoadError(
                    f"agent file {file.name}: declares 'name: {agent.name}' "
                    f"but the filename stem is {file.stem!r} (they must match)",
                    source,  # type: ignore[arg-type]
                )
            )

    for phase in workflow.phases:
        for step in phase.steps:
            check(step.file, step.source)
        for gate in phase.gates:
            if isinstance(gate, AgentGate):
                check(gate.file, gate.source)

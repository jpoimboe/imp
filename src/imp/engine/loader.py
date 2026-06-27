"""Workflow and agent loaders.

Build order in this file:
  Task 4 - parse_agent_file
  Tasks 5-7 - workflow YAML parsing (now unified step model, cutover 2/6)
  Task 8 - source location tracking
"""

from __future__ import annotations

import dataclasses
import re
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import yaml

from imp.engine._yaml import LineLoader, loc
from imp.engine._yaml import items as _items
from imp.engine.differ_graph import find_differ_from_cycle
from imp.engine.drivers import KNOWN_DRIVERS
from imp.engine.errors import LoadError, LoadErrors
from imp.engine.expr import (
    Binary,
    BoolLit,
    Expr,
    ExprError,
    ExprType,
    IntLit,
    Name,
    StrLit,
    TypeEnv,
    require_bool,
    require_type,
)
from imp.engine.expr import (
    parse as parse_expr,
)
from imp.engine.schema import parse_output_schema
from imp.engine.types import (
    Action,
    AgentDef,
    Continue,
    Goto,
    Kind,
    Mutation,
    Pause,
    Phase,
    Rule,
    SourceLoc,
    Step,
    VarDecl,
    Vars,
    Workflow,
    WorkflowDefaults,
)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _split_frontmatter(text: str, source_path: Path) -> tuple[str, str, int]:
    """Returns (frontmatter_text, body_text, body_start_line).

    body_start_line is the 1-indexed line in the file where the body begins,
    used so prompt-template error tracebacks can be correct later.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise LoadError(
            "agent file must start with YAML frontmatter delimited by '---'",
            SourceLoc(file=source_path, line=1),
        )
    fm_text = m.group(1)
    body_text = m.group(2)
    fm_lines = fm_text.count("\n") + 1
    body_start = 1 + 1 + fm_lines + 1
    return fm_text, body_text, body_start


def parse_agent_file(path: Path) -> AgentDef:
    text = path.read_text()
    fm_text, body, _body_line = _split_frontmatter(text, path)
    try:
        fm: dict[str, Any] = yaml.load(fm_text, Loader=LineLoader) or {}
    except yaml.YAMLError as e:
        raise LoadError(f"invalid YAML in frontmatter: {e}", SourceLoc(file=path, line=1)) from e
    if not isinstance(fm, dict):
        raise LoadError("frontmatter must be a mapping", SourceLoc(file=path, line=1))

    source = loc(path, fm)

    if "name" not in fm:
        raise LoadError("missing 'name' in agent frontmatter", source)
    if "output" not in fm:
        raise LoadError("missing 'output' in agent frontmatter", source)

    output_raw = fm["output"]
    if not isinstance(output_raw, dict):
        raise LoadError(
            "'output' must be a mapping", source
        )
    schema = parse_output_schema(output_raw, loc(path, output_raw))
    if "tools" in fm and "deny_tools" in fm:
        raise LoadError(
            "agent may declare 'tools:' or 'deny_tools:', not both", source)
    explicit_tools = "tools" in fm
    tools = frozenset(fm.get("tools") or [])
    deny_raw = fm.get("deny_tools")
    if deny_raw is not None and not (
        isinstance(deny_raw, list) and all(isinstance(x, str) for x in deny_raw)
    ):
        raise LoadError("'deny_tools' must be a list of strings", source)
    deny_tools = frozenset(deny_raw or [])
    unknown_deny = deny_tools - DENYABLE_TOOLS
    if unknown_deny:
        raise LoadError(
            "'deny_tools' names tools that are never default-granted "
            f"(no-op): {sorted(unknown_deny)}; denyable tools are "
            f"{sorted(DENYABLE_TOOLS)}",
            source,
        )
    subagents = tuple(fm.get("subagents") or [])

    drivers_raw = fm.get("drivers")
    if drivers_raw is not None and not (
        isinstance(drivers_raw, list) and all(isinstance(x, str) for x in drivers_raw)
    ):
        raise LoadError("'drivers' must be a list of strings", source)
    drivers = frozenset(drivers_raw or [])
    unknown_drivers = drivers - KNOWN_DRIVERS
    if unknown_drivers:
        raise LoadError(
            f"'drivers' names unknown drivers {sorted(unknown_drivers)}; "
            f"known drivers are {sorted(KNOWN_DRIVERS)}",
            source,
        )

    mtc_raw = fm.get("max_tool_calls")
    if mtc_raw is not None and (
        not isinstance(mtc_raw, int) or isinstance(mtc_raw, bool) or mtc_raw <= 0
    ):
        raise LoadError(
            f"'max_tool_calls' must be a positive integer; got {mtc_raw!r}",
            source,
        )

    tier_raw = fm.get("tier")
    tier = str(tier_raw) if tier_raw is not None else None

    label_raw = fm.get("label")
    if label_raw is not None and not isinstance(label_raw, str):
        raise LoadError("an agent's `label:` must be a string", source)

    return AgentDef(
        name=str(fm["name"]),
        description=str(fm.get("description", "")),
        tools=tools,
        subagents=subagents,
        output_schema=schema,
        prompt_template_source=body,
        source_path=path,
        source=source,
        max_tool_calls=mtc_raw,
        tier=tier,
        explicit_tools=explicit_tools,
        deny_tools=deny_tools,
        drivers=drivers,
        label=label_raw,
    )


BASE_CAPS: frozenset[str] = frozenset({
    "read_file", "glob", "grep", "query_events", "bash",
    "edit_file", "write_file", "web_search", "web_fetch",
    "update_step_label", "emit_notice", "symbol_outline",
})
TOP_LEVEL_CAPS: frozenset[str] = frozenset(
    {"rename_change", "create_change", "create_and_enter_worktree"})

# The maximal default grant any agent can receive. deny_tools only ever
# subtracts from this set, so a deny entry outside it is a silent no-op
# (almost always a typo). Validated at parse time. Subagents receive a
# subset of this, so denying e.g. rename_change on a subagent is a harmless
# no-op rather than an error — the name is real, just not in that context.
DENYABLE_TOOLS: frozenset[str] = BASE_CAPS | TOP_LEVEL_CAPS | frozenset({"sub_agent"})


def _effective_tools(agent: AgentDef, is_sub: bool) -> frozenset[str]:
    if agent.explicit_tools:
        return frozenset(agent.tools)
    effective = set(BASE_CAPS)
    if not is_sub:
        effective |= TOP_LEVEL_CAPS
        if agent.subagents:
            effective.add("sub_agent")
    effective -= agent.deny_tools
    return frozenset(effective)


def _resolve_agent_tools(agents: dict[str, AgentDef]) -> None:
    dispatchable: set[str] = set()
    for a in agents.values():
        dispatchable.update(a.subagents)
    for name in list(agents):
        agent = agents[name]
        agents[name] = dataclasses.replace(
            agent, tools=_effective_tools(agent, name in dispatchable))


# ── Workflow parsing ──────────────────────────────────────────────────────────

DEFAULT_MAX_RETRIES = 3
DEFAULT_ON_MAX_RETRIES: Action = Pause()

# Map agent output-schema field types to ExprType.
_SCHEMA_TO_EXPR: dict[str, ExprType] = {
    "int": "int",
    "string": "string",
    "bool": "bool",
    "enum": "string",
}
_SCRIPT_OUTPUT_TYPES: dict[str, ExprType] = {
    "exit": "int",
    "stdout": "string",
    "stderr": "string",
}


def _output_types_for_step(
    step_kind: str, agent: AgentDef | None
) -> dict[str, ExprType] | None:
    """The expr output-env types for one step.

    None means no typed output env (branch, or agent step with no resolved agent).
    """
    if step_kind == "agent" and agent is not None:
        out: dict[str, ExprType] = {}
        for f in agent.output_schema.fields:
            if f.type == "object":
                continue  # object outputs are not addressable in if:/set: (deferred)
            out[f.name] = _SCHEMA_TO_EXPR[f.type]
        return out
    if step_kind == "script":
        return dict(_SCRIPT_OUTPUT_TYPES)
    if step_kind == "merge_and_exit_worktree":
        return {"merge": "string"}   # engine produces merge: MERGED|CONFLICT|REFUSED|DIRTY
    return None  # branch / engine-native (no typed output env)


# ── Kind inference ────────────────────────────────────────────────────────────

_KIND_SIGNALS = {
    "agent": ("file", "prompt"),
    "script": ("command",),
}
_ENGINE_NATIVE = ("merge_and_exit_worktree",)


def _infer_kind(raw: dict[str, Any], source: SourceLoc) -> str:
    present = {
        kind for kind, keys in _KIND_SIGNALS.items() if any(k in raw for k in keys)
    }
    has_rules = "rules" in raw
    explicit = raw.get("kind")
    if explicit in _ENGINE_NATIVE:
        if present:
            raise LoadError(
                f"{explicit} step carries no file/command (engine-native)", source)
        return str(explicit)
    if len(present) > 1:
        raise LoadError(
            f"step has conflicting kind signals {sorted(present)}; "
            "a node carries exactly one of file/prompt, command",
            source,
        )
    inferred = next(iter(present), None) or ("branch" if has_rules else None)
    if inferred is None:
        raise LoadError(
            "step has no kind signal (need one of file/prompt, command, or rules)",
            source,
        )
    if explicit is not None and explicit != inferred:
        raise LoadError(
            f"declared kind {explicit!r} disagrees with the keys present "
            f"(inferred {inferred!r})",
            source,
        )
    return inferred


# ── Expression parsing ────────────────────────────────────────────────────────


def _typed_literal(value: Any, source: SourceLoc) -> Expr:
    if isinstance(value, bool):
        return BoolLit(value)
    if isinstance(value, int):
        return IntLit(value)
    if isinstance(value, str):
        return StrLit(value)
    raise LoadError(f"unsupported literal {value!r} in if: mapping", source)


def _parse_if(raw_if: Any, source: SourceLoc) -> Expr:
    """`if:` is either a mapping (typed-literal equality, ANDed) or a string expr."""
    if isinstance(raw_if, str):
        try:
            return parse_expr(raw_if)
        except ExprError as e:
            raise LoadError(f"invalid expression: {e}", source) from e
    if isinstance(raw_if, dict):
        terms: list[Expr] = []
        for k, v in _items(raw_if):
            terms.append(Binary("==", Name(str(k)), _typed_literal(v, source)))
        if not terms:
            raise LoadError("if: mapping is empty", source)
        expr = terms[0]
        for t in terms[1:]:
            expr = Binary("and", expr, t)
        return expr
    raise LoadError("if: must be a mapping or a string expression", source)


def _parse_set(raw_set: Any, source: SourceLoc) -> Mutation:
    if not isinstance(raw_set, dict):
        raise LoadError("set: must be a mapping of var to expression", source)
    pairs: list[tuple[str, Expr]] = []
    for var_name, rhs in _items(raw_set):
        expr = _coerce_rhs(rhs, source)
        pairs.append((str(var_name), expr))
    return tuple(pairs)


def _coerce_rhs(rhs: Any, source: SourceLoc) -> Expr:
    """A set: RHS is a string expression, or a bare typed literal."""
    if isinstance(rhs, str):
        try:
            return parse_expr(rhs)
        except ExprError as e:
            raise LoadError(f"invalid set: expression: {e}", source) from e
    return _typed_literal(rhs, source)


# ── Action verb + rule parsing ────────────────────────────────────────────────


def _parse_verb(raw: dict[str, Any], source: SourceLoc) -> Action:
    verbs = [v for v in ("continue", "goto", "pause", "resume") if v in raw]
    if len(verbs) != 1:
        raise LoadError(
            f"a rule/option needs exactly one action verb "
            f"(continue/goto/pause/resume); found {verbs}",
            source,
        )
    verb = verbs[0]
    if verb == "continue":
        if raw["continue"] is not True:
            raise LoadError("continue: must be true", source)
        return Continue()
    if verb == "pause":
        if raw["pause"] is not True:
            raise LoadError("pause: must be true", source)
        return Pause()
    # goto: and resume: both route to a target Goto; resume continues the transcript.
    target = raw[verb]
    if not isinstance(target, str):
        raise LoadError(f"{verb}: must name a phase or step", source)
    feedback = raw.get("feedback")
    if feedback is not None and not isinstance(feedback, str):
        raise LoadError(
            "feedback: must be a string preamble (the bool form was removed in §6)",
            source,
        )
    return Goto(target=target, feedback=feedback, resume=(verb == "resume"))


def _parse_rules(
    raw_rules: Any, source: SourceLoc, yaml_path: Path
) -> tuple[Rule, ...]:
    if not isinstance(raw_rules, list) or not raw_rules:
        raise LoadError("rules: must be a non-empty list", source)
    rules: list[Rule] = []
    for entry in raw_rules:
        if not isinstance(entry, dict):
            raise LoadError("each rule must be a mapping", source)
        rsrc = loc(yaml_path, entry)
        action = _parse_verb(entry, rsrc)
        release = _parse_lane_attr(entry, "release", rsrc)
        if release is not None:
            if not isinstance(action, Goto):
                raise LoadError(
                    "release: only valid alongside a goto:/resume: route", rsrc)
            action = replace(action, release=release)
        mutations = _parse_set(entry["set"], rsrc) if "set" in entry else ()
        if entry.get("else") is True:
            condition: Expr | None = None
        elif "if" in entry:
            condition = _parse_if(entry["if"], rsrc)
        else:
            raise LoadError("a rule needs `if:` or `else: true`", rsrc)
        label = entry.get("label")
        if label is not None and not isinstance(label, str):
            raise LoadError("a rule's `label:` must be a string", rsrc)
        rules.append(Rule(condition=condition, action=action,
                          mutations=mutations, source=rsrc, label=label))
    return tuple(rules)


# ── Vars parsing ──────────────────────────────────────────────────────────────

_VAR_TYPES: dict[str, ExprType] = {"bool": "bool", "int": "int", "string": "string"}


def _parse_vars(raw: Any, source: SourceLoc, yaml_path: Path) -> Vars:
    if raw is None:
        return Vars(decls=())
    if not isinstance(raw, dict):
        raise LoadError("vars: must be a mapping of name to {type, default}", source)
    decls: list[VarDecl] = []
    for name, spec in _items(raw):
        vsrc = loc(yaml_path, spec) if isinstance(spec, dict) else source
        if not isinstance(spec, dict) or "type" not in spec:
            raise LoadError(f"var {name!r} needs a 'type'", vsrc)
        vtype = spec["type"]
        if vtype not in _VAR_TYPES:
            raise LoadError(
                f"var {name!r} type must be bool/int/string, got {vtype!r}", vsrc)
        default = spec.get("default", {"bool": False, "int": 0, "string": ""}[vtype])
        _check_default_type(name, vtype, default, vsrc)
        decls.append(VarDecl(name=str(name), type=_VAR_TYPES[vtype],
                             default=default, source=vsrc))
    return Vars(decls=tuple(decls))


def _check_default_type(name: str, vtype: str, default: object, source: SourceLoc) -> None:
    ok = (
        (vtype == "bool" and isinstance(default, bool))
        or (vtype == "int" and isinstance(default, int) and not isinstance(default, bool))
        or (vtype == "string" and isinstance(default, str))
    )
    if not ok:
        raise LoadError(
            f"var {name!r} default {default!r} does not match type {vtype}", source)


# ── Int resolution ────────────────────────────────────────────────────────────


def _resolve_int(
    raw: dict[str, Any], key: str, default: int | None, source: SourceLoc, where: str
) -> int | None:
    """Resolve an integer knob with null tolerance; reject non-int / bool / negative."""
    if key not in raw:
        return default
    val = raw[key]
    if val is None:
        return default
    if not isinstance(val, int) or isinstance(val, bool):
        raise LoadError(
            f"{where}: {key!r} must be an integer, got {type(val).__name__}",
            source,
        )
    if val < 0:
        raise LoadError(f"{where}: {key!r} must be >= 0", source)
    return val


def _resolve_max_retries(
    raw: dict[str, Any], default: int, source: SourceLoc, where: str
) -> int:
    """Resolve max_retries with null tolerance; reject non-int values cleanly."""
    result = _resolve_int(raw, "max_retries", default, source, where)
    return result if result is not None else default


# ── Defaults parsing ──────────────────────────────────────────────────────────


def _parse_defaults(
    raw: dict[str, Any] | None, source: SourceLoc, yaml_path: Path | None = None
) -> WorkflowDefaults:
    if raw is None:
        return WorkflowDefaults(
            max_retries=DEFAULT_MAX_RETRIES,
            on_max_retries=DEFAULT_ON_MAX_RETRIES,
        )
    defaults_source = loc(yaml_path, raw) if yaml_path is not None else source
    max_retries = _resolve_max_retries(raw, DEFAULT_MAX_RETRIES, defaults_source, "defaults")
    on_max = (
        _parse_on_max_retries(raw["on_max_retries"], defaults_source)
        if "on_max_retries" in raw
        else DEFAULT_ON_MAX_RETRIES
    )
    mtc_raw = raw.get("max_tool_calls")
    if mtc_raw is not None and (
        not isinstance(mtc_raw, int) or isinstance(mtc_raw, bool) or mtc_raw <= 0
    ):
        raise LoadError(
            f"'max_tool_calls' must be a positive integer; got {mtc_raw!r}",
            defaults_source,
        )
    max_loops_val = _resolve_int(raw, "max_loops", 10, defaults_source, "defaults")
    max_loops = max_loops_val if max_loops_val is not None else 10
    return WorkflowDefaults(
        max_retries=max_retries,
        on_max_retries=on_max,
        max_tool_calls=mtc_raw,
        max_loops=max_loops,
    )


def _parse_on_max_retries(raw: Any, source: SourceLoc) -> Action:
    """Parse defaults.on_max_retries as a new verb-style mapping
    ({pause: true} / {continue: true} / {goto: target})."""
    if not isinstance(raw, dict):
        raise LoadError(
            f"on_max_retries must be a mapping, got {type(raw).__name__}", source
        )
    return _parse_verb(raw, source)


# ── Type-checking ─────────────────────────────────────────────────────────────


def _typecheck_step_exprs(
    step: Step,
    agents: dict[str, AgentDef],
    var_types: dict[str, ExprType],
    source: SourceLoc,
) -> None:
    agent = agents.get(step.file.stem) if (step.kind == "agent" and step.file) else None
    output_types = _output_types_for_step(step.kind, agent)
    env = TypeEnv(vars=var_types, output=output_types)
    try:
        for rule in step.rules:
            if rule.condition is not None:
                require_bool(rule.condition, env)
            for mut_name, mut_expr in rule.mutations:
                if mut_name not in var_types:
                    raise LoadError(
                        f"set: target {mut_name!r} is not a declared var", rule.source
                    )
                require_type(mut_expr, env, var_types[mut_name])
    except LoadError:
        raise
    except ExprError as e:
        raise LoadError(str(e), step.source) from e


# ── Step parsing ──────────────────────────────────────────────────────────────


def _parse_lane_attr(raw: dict[str, Any], key: str, source: SourceLoc) -> str | None:
    val = raw.get(key)
    if val is None:
        return None
    if val != "base":
        raise LoadError(
            f"{key}: only the lane keyword 'base' is supported, got {val!r}",
            source)
    return "base"


def _parse_bool_flag(raw: dict[str, Any], key: str, source: SourceLoc) -> bool:
    val = raw.get(key, False)
    if not isinstance(val, bool):
        raise LoadError(
            f"{key!r} must be a boolean, got {type(val).__name__}", source)
    return val


def _parse_phase_chrome(praw: dict[str, Any], source: SourceLoc) -> str | None:
    chrome = praw.get("chrome")
    if chrome is None:
        return None
    if chrome != "none":
        raise LoadError("a phase's `chrome:` only accepts the value `none`", source)
    return "none"


def _parse_phase_label(praw: dict[str, Any], source: SourceLoc) -> str | None:
    label = praw.get("label")
    if label is not None and not isinstance(label, str):
        raise LoadError("a phase's `label:` must be a string", source)
    return label


def _parse_step(
    raw: dict[str, Any],
    phase_name: str,
    workflow_dir: Path,
    defaults: WorkflowDefaults,
    agents: dict[str, AgentDef],
    var_types: dict[str, ExprType],
    yaml_path: Path,
    chrome: str | None = None,
    phase_label: str | None = None,
) -> Step:
    fallback = SourceLoc(file=yaml_path, line=1)
    if not isinstance(raw, dict):
        raise LoadError(f"step must be a mapping, got {type(raw).__name__}", fallback)
    source = loc(yaml_path, raw)
    if not raw.get("name") or not isinstance(raw["name"], str):
        raise LoadError("step missing a string 'name'", source)
    name = raw["name"]
    kind = _infer_kind(raw, source)
    quiet = _parse_bool_flag(raw, "quiet", source)
    position_label = _parse_bool_flag(raw, "position_label", source)
    label = raw.get("label")
    if label is not None and not isinstance(label, str):
        raise LoadError("a step's `label:` must be a string", source)
    if label is None and kind == "agent" and raw.get("file"):
        agent_def = agents.get(Path(raw["file"]).stem)
        if agent_def is not None:
            label = agent_def.label

    acquire = _parse_lane_attr(raw, "acquire", source)
    release = _parse_lane_attr(raw, "release", source)
    if acquire is not None and kind != "branch":
        raise LoadError(
            f"acquire: only valid on a rules-only branch step, not a {kind} step",
            source)
    if release is not None and kind != "agent":
        raise LoadError(
            f"release: only valid on an agent step, not a {kind} step", source)

    max_loops_val = _resolve_int(raw, "max_loops", defaults.max_loops, source, name)
    max_loops = max_loops_val if max_loops_val is not None else defaults.max_loops
    on_max = defaults.on_max_retries  # on_max_retries is not a per-step YAML knob

    rules: tuple[Rule, ...] = ()

    if "rules" in raw:
        rules = _parse_rules(raw["rules"], source, yaml_path)
    elif kind in ("script", "branch", "merge_and_exit_worktree"):
        raise LoadError(f"{kind} step {name!r} requires a rules: block", source)
    # agent with no rules means continue-on-success (rules stays ())

    if kind == "agent":
        file = (workflow_dir / raw["file"]) if raw.get("file") else None
        step = Step(
            name=name, phase=phase_name, kind="agent", rules=rules,
            on_max_retries=on_max, max_loops=max_loops, source=source,
            file=file, prompt=(str(raw["prompt"]) if raw.get("prompt") else None),
            tier=str(raw.get("tier") or "medium"),
            differ_from=(str(raw["differ_from"])
                         if raw.get("differ_from") is not None else None),
            read_only=tuple(str(p) for p in (raw.get("read_only") or [])),
            max_tool_calls=raw.get("max_tool_calls"),
            max_retries=_resolve_max_retries(raw, defaults.max_retries, source, name),
            quiet=quiet,
            position_label=position_label,
            label=label,
            acquire=acquire,
            release=release,
            chrome=chrome,
            phase_label=phase_label,
        )
    elif kind == "script":
        step = Step(
            name=name, phase=phase_name, kind="script", rules=rules,
            on_max_retries=on_max, max_loops=max_loops, source=source,
            command=str(raw["command"]),
            cwd=str(raw["cwd"]) if raw.get("cwd") else None,
            timeout_seconds=raw.get("timeout"),
            quiet=quiet,
            label=label,
            chrome=chrome,
            phase_label=phase_label,
        )
    elif kind in ("merge_and_exit_worktree",):
        step = Step(
            name=name, phase=phase_name, kind=cast(Kind, kind), rules=rules,
            on_max_retries=on_max, max_loops=max_loops, source=source,
            quiet=quiet,
            label=label,
            chrome=chrome,
            phase_label=phase_label,
        )
    else:  # branch
        step = Step(
            name=name, phase=phase_name, kind="branch", rules=rules,
            on_max_retries=on_max, max_loops=max_loops, source=source,
            quiet=quiet,
            label=label,
            acquire=acquire,
            release=release,
            chrome=chrome,
            phase_label=phase_label,
        )

    # Type-check every if:/set: against this step's env (output and vars).
    _typecheck_step_exprs(step, agents, var_types, source)
    return step


# ── Workflow loader ───────────────────────────────────────────────────────────


def load_workflow(
    workflow_dir: Path,
    configured_tiers: frozenset[str] | None = None,
    project_keys: frozenset[str] = frozenset(),
) -> Workflow:
    """Parse <workflow_dir>/workflow.yaml + agents/*.md into a Workflow."""
    yaml_path = workflow_dir / "workflow.yaml"
    if not yaml_path.exists():
        raise LoadError(
            f"workflow.yaml not found in {workflow_dir}",
            SourceLoc(file=yaml_path, line=1),
        )
    try:
        raw = yaml.load(yaml_path.read_text(), Loader=LineLoader) or {}
    except yaml.YAMLError as e:
        raise LoadError(
            f"invalid YAML: {e}", SourceLoc(file=yaml_path, line=1)
        ) from e
    if not isinstance(raw, dict):
        raise LoadError(
            "workflow.yaml root must be a mapping",
            SourceLoc(file=yaml_path, line=1),
        )

    root_source = loc(yaml_path, raw)
    if "phases" not in raw:
        raise LoadError("missing 'phases' list", root_source)

    defaults = _parse_defaults(raw.get("defaults"), root_source, yaml_path)

    # Pre-parse agent files in agents/ so steps can reference by relative path.
    agents: dict[str, AgentDef] = {}
    agents_dir = workflow_dir / "agents"
    if agents_dir.is_dir():
        for md in sorted(agents_dir.glob("*.md")):
            agent = parse_agent_file(md)
            if agent.name in agents:
                raise LoadError(
                    f"duplicate agent name {agent.name!r} "
                    f"(also declared in {agents[agent.name].source_path})",
                    agent.source,
                )
            agents[agent.name] = agent

    _resolve_agent_tools(agents)

    # vars (top-level, optional)
    vars_obj = _parse_vars(raw.get("vars"), root_source, yaml_path)
    var_types = vars_obj.types()

    # phases: inline list-of-objects
    raw_phases = raw["phases"]
    if not isinstance(raw_phases, list) or not raw_phases:
        raise LoadError("'phases' must be a non-empty list of {name, steps}", root_source)
    phase_objs: list[Phase] = []
    for praw in raw_phases:
        if not isinstance(praw, dict) or "name" not in praw or "steps" not in praw:
            raise LoadError("each phase needs 'name' and 'steps'", root_source)
        psrc = loc(yaml_path, praw)
        pname = str(praw["name"])
        steps_raw = praw["steps"]
        if not isinstance(steps_raw, list):
            raise LoadError(f"phase {pname!r}: 'steps' must be a list", psrc)
        phase_chrome = _parse_phase_chrome(praw, psrc)
        phase_label = _parse_phase_label(praw, psrc)
        steps = tuple(
            _parse_step(s, pname, workflow_dir, defaults, agents, var_types,
                        yaml_path, chrome=phase_chrome, phase_label=phase_label)
            for s in steps_raw
        )
        phase_objs.append(Phase(name=pname, steps=steps, source=psrc, label=phase_label))

    # Best-effort cycle pre-check on the workflow's per-step differ_from DAG.
    # The authoritative validator is in providers.resolve.resolve_run; this
    # gives a fast authoring error at load time.
    edges = {
        step.name: step.differ_from
        for step in (s for p in phase_objs for s in p.steps)
        if step.kind == "agent" and step.differ_from is not None
    }
    cyc = find_differ_from_cycle(edges)
    if cyc is not None:
        raise LoadError(
            "differ_from cycle: " + " -> ".join(cyc),
            root_source,
        )

    persona_path = workflow_dir / "persona.md"
    persona_source = persona_path.read_text() if persona_path.is_file() else ""

    workflow = Workflow(
        name=str(raw.get("name", workflow_dir.name)),
        description=str(raw.get("description", "")),
        phases=tuple(phase_objs),
        vars=vars_obj,
        defaults=defaults,
        agents=agents,
        workflow_dir=workflow_dir,
        source=root_source,
        persona_source=persona_source,
    )

    # Run validators and print warnings; raise on errors.
    from imp.engine.validators import validate
    errors, warnings = validate(
        workflow, configured_tiers=configured_tiers, project_keys=project_keys)
    if errors:
        raise LoadErrors(errors=errors, warnings=warnings)
    for w in warnings:
        print(str(w), file=sys.stderr)
    return workflow

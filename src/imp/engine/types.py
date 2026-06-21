"""Frozen workflow domain model.

Every node parsed from workflow.yaml or agent .md carries a SourceLoc so error
messages can reference file:line.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from imp.engine.expr import Expr, ExprType


@dataclass(frozen=True, slots=True)
class SourceLoc:
    file: Path
    line: int

    def __str__(self) -> str:
        return f"{self.file}:{self.line}"


# ── Actions (internal -- never serialized into YAML) ─────────────────────────

@dataclass(frozen=True, slots=True)
class Action:
    """Base for Continue/Retry/Pause/Goto. Use match/case to dispatch."""


@dataclass(frozen=True, slots=True)
class Continue(Action):
    pass


@dataclass(frozen=True, slots=True)
class Retry(Action):
    """Engine-internal error recovery only -- never authored in YAML."""


@dataclass(frozen=True, slots=True)
class Pause(Action):
    reason: str | None = None
    kind: str | None = None


@dataclass(frozen=True, slots=True)
class Goto(Action):
    target: str
    feedback: str | None = None  # preamble string; the engine prepends it and
                                 # appends the prior step's structured output. None
                                 # => forwards nothing on this edge (§6).
    resume: bool = False         # set by the `resume:` verb; continues the transcript


# ── Mutations & rules ─────────────────────────────────────────────────────────

# A set: clause -- ordered (var_name, expression) pairs applied before a jump.
Mutation = tuple[tuple[str, Expr], ...]


@dataclass(frozen=True, slots=True)
class Rule:
    """One rule in a rules: block. `condition is None` means else (catch-all)."""
    condition: Expr | None
    action: Action
    mutations: Mutation
    source: SourceLoc
    label: str | None = None        # workflow-authored route reason; engine-opaque

    @property
    def is_else(self) -> bool:
        return self.condition is None


# ── Variables ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class VarDecl:
    name: str
    type: ExprType                 # "int" | "bool" | "string"
    default: object
    source: SourceLoc


@dataclass(frozen=True, slots=True)
class Vars:
    decls: tuple[VarDecl, ...]

    def types(self) -> dict[str, ExprType]:
        return {d.name: d.type for d in self.decls}

    def defaults(self) -> dict[str, object]:
        return {d.name: d.default for d in self.decls}

    def names(self) -> frozenset[str]:
        return frozenset(d.name for d in self.decls)


# ── Output schema (agent's structured output contract) ────────────────────────

@dataclass(frozen=True, slots=True)
class OutputField:
    name: str
    type: Literal["string", "int", "bool", "enum", "object"]
    values: tuple[str, ...] = ()  # for enum
    required: bool = True
    description: str = ""


@dataclass(frozen=True, slots=True)
class OutputSchema:
    fields: tuple[OutputField, ...]
    source: SourceLoc

    def status_field(self) -> OutputField | None:
        for f in self.fields:
            if f.name == "status":
                return f
        return None

    def field_names(self) -> frozenset[str]:
        return frozenset(f.name for f in self.fields)


# ── Filesystem permissions (parsed but unused in Plan 1; validated only) ──────

@dataclass(frozen=True, slots=True)
class FsRule:
    operations: tuple[Literal["read", "write"], ...]
    paths: tuple[str, ...]
    mode: Literal["allow", "deny"]
    source: SourceLoc


# ── Agent definition (parsed from .md) ────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class AgentDef:
    name: str
    description: str
    tools: frozenset[str]
    subagents: tuple[str, ...]              # subagent names; Plan 2 resolves to full defs
    output_schema: OutputSchema
    prompt_template_source: str             # the .md body before compilation
    source_path: Path
    source: SourceLoc
    max_tool_calls: int | None = None
    # Capability tier this agent wants WHEN RUN AS A SUBAGENT. MUST default to
    # None so dispatch_task can tell "inherit parent" (None) from "use the
    # 'medium' tier". Top-level steps override at the step level via Step.tier.
    tier: str | None = None
    explicit_tools: bool = False
    deny_tools: frozenset[str] = frozenset()
    drivers: frozenset[str] = frozenset()
    label: str | None = None


# ── Steps & phases ────────────────────────────────────────────────────────────

Kind = Literal["agent", "script", "branch", "merge", "wait_for_head", "dequeue_wake"]


@dataclass(frozen=True, slots=True)
class Step:
    name: str                       # globally unique; disjoint from phase names
    phase: str                      # owning container (set by loader from structure)
    kind: Kind
    rules: tuple[Rule, ...]         # required for script/branch; optional for agent
    on_max_retries: Action          # action on max_loops (and, for agents, max_retries) exhaustion
    max_loops: int
    source: SourceLoc
    # agent-only
    file: Path | None = None
    prompt: str | None = None
    tier: str = "medium"
    differ_from: str | None = None      # name of another step in this workflow;
                                        # this step's resolved (provider, model)
                                        # is scored to differ from that step's.
    fs_permissions: tuple[FsRule, ...] = ()
    max_tool_calls: int | None = None
    max_retries: int = 0
    # script-only
    command: str | None = None
    cwd: str | None = None
    timeout_seconds: int | None = None
    quiet: bool = False
    label: str | None = None
    acquire: str | None = None  # lane keyword "base" → change.base_branch; branch steps only
    release: str | None = None  # runs before step body; agent steps only


@dataclass(frozen=True, slots=True)
class Phase:
    name: str
    steps: tuple[Step, ...]
    source: SourceLoc


# ── Workflow root ─────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class WorkflowDefaults:
    max_retries: int
    on_max_retries: Action
    max_tool_calls: int | None = None
    max_loops: int = 10


@dataclass(frozen=True, slots=True)
class Workflow:
    """`agents` is a plain dict for V0; callers must treat it as immutable."""
    name: str
    description: str
    phases: tuple[Phase, ...]
    vars: Vars
    defaults: WorkflowDefaults
    agents: dict[str, AgentDef] = field(hash=False)
    workflow_dir: Path
    source: SourceLoc

    def flat_steps(self) -> tuple[Step, ...]:
        return tuple(s for p in self.phases for s in p.steps)

    def step_index(self, name: str) -> int | None:
        for i, s in enumerate(self.flat_steps()):
            if s.name == name:
                return i
        return None

    def phase_of(self, step_name: str) -> str | None:
        for p in self.phases:
            for s in p.steps:
                if s.name == step_name:
                    return p.name
        return None

    def resolve_goto(self, target: str) -> int:
        """Flat index of a goto target. A phase target resolves to its first
        step's flat index; an EMPTY phase target resolves to len(flat_steps())
        -- an out-of-range index the executor reads as 'terminate' (an empty
        phase is always trailing, guaranteed by validation). Raises KeyError on
        an unknown target (validation forbids that statically)."""
        flat = self.flat_steps()
        for i, s in enumerate(flat):
            if s.name == target:
                return i
        for p in self.phases:
            if p.name == target:
                if not p.steps:
                    return len(flat)
                first_name = p.steps[0].name
                for i, s in enumerate(flat):
                    if s.name == first_name:
                        return i
        raise KeyError(target)

"""Frozen workflow domain model.

Every node parsed from workflow.yaml or agent .md carries a SourceLoc so error
messages can reference file:line.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class SourceLoc:
    file: Path
    line: int

    def __str__(self) -> str:
        return f"{self.file}:{self.line}"


# ── Actions ────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Action:
    """Base for Continue/Retry/Pause/Goto. Use match/case to dispatch."""


@dataclass(frozen=True, slots=True)
class Continue(Action):
    pass


@dataclass(frozen=True, slots=True)
class Retry(Action):
    pass


@dataclass(frozen=True, slots=True)
class Pause(Action):
    pass


@dataclass(frozen=True, slots=True)
class Goto(Action):
    target: str


# ── Decide blocks ──────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Rule:
    """One rule inside a decide: block.

    Exactly one of `when` (non-empty dict of field→value to match) or `is_else`
    (catch-all) must be set.
    """
    when: tuple[tuple[str, object], ...]  # immutable dict-as-pairs; () for else
    is_else: bool
    action: Action
    source: SourceLoc

    def __post_init__(self) -> None:
        if self.is_else and self.when:
            raise ValueError("Rule: is_else=True must have empty when")
        if not self.is_else and not self.when:
            raise ValueError(
                "Rule: empty when without is_else=True is a black hole"
            )


@dataclass(frozen=True, slots=True)
class Decide:
    rules: tuple[Rule, ...]
    source: SourceLoc


# ── Output schema (agent's structured output contract) ─────────────────────

@dataclass(frozen=True, slots=True)
class OutputField:
    name: str
    type: Literal["string", "int", "bool", "enum", "object"]
    values: tuple[str, ...] = ()  # for enum
    required: bool = True


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


# ── Filesystem permissions (parsed but unused in Plan 1; validated only) ───

@dataclass(frozen=True, slots=True)
class FsRule:
    operations: tuple[Literal["read", "write"], ...]
    paths: tuple[str, ...]
    mode: Literal["allow", "deny"]
    source: SourceLoc


# ── Gates ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Gate:
    """Base for gates. Concrete subclasses below."""
    source: SourceLoc


@dataclass(frozen=True, slots=True)
class HumanOption:
    label: str
    action: Action
    source: SourceLoc


@dataclass(frozen=True, slots=True)
class HumanGate(Gate):
    prompt: str
    options: tuple[HumanOption, ...]


@dataclass(frozen=True, slots=True)
class ScriptGate(Gate):
    command: str
    cwd: str | None
    timeout_seconds: int | None
    decide: Decide  # default decide is constructed by loader when omitted


@dataclass(frozen=True, slots=True)
class AgentGate(Gate):
    """Agent-as-gate. Plan 1 parses + validates this but does NOT execute it
    (no real LLM yet). The executor raises NotImplementedError on dispatch.
    """
    file: Path
    model_role: str
    fs_permissions: tuple[FsRule, ...]
    decide: Decide


@dataclass(frozen=True, slots=True)
class StubGate(Gate):
    """Test-only gate that returns a fixed action. Not exposed in workflow YAML;
    used by unit tests to exercise the executor without spinning up agents.
    """
    action: Action


# ── Steps & phases ─────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Step:
    name: str
    file: Path                              # absolute path into workflow snapshot
    interactive: bool
    model_role: str
    fs_permissions: tuple[FsRule, ...]
    gates: tuple[Gate, ...]                 # () = implicit continue-on-success
    max_retries: int                        # resolved from defaults if omitted
    on_max_retries: Action                  # resolved from defaults if omitted
    source: SourceLoc


@dataclass(frozen=True, slots=True)
class Phase:
    name: str
    steps: tuple[Step, ...]
    gates: tuple[Gate, ...]                 # phase-level gates run after all steps
    max_retries: int
    on_max_retries: Action
    source: SourceLoc


# ── Agent definition (parsed from .md) ─────────────────────────────────────

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


# ── Workflow root ──────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class WorkflowDefaults:
    max_retries: int
    on_max_retries: Action


@dataclass(frozen=True, slots=True)
class Workflow:
    """`agents` is a plain dict for V0; callers must treat it as immutable.
    Plan 2 will wrap it in `MappingProxyType` for runtime enforcement.
    """
    name: str
    description: str
    phases: tuple[Phase, ...]
    defaults: WorkflowDefaults
    agents: dict[str, AgentDef] = field(hash=False)  # name → def
    workflow_dir: Path
    source: SourceLoc

    def phase_index(self, name: str) -> int | None:
        for i, p in enumerate(self.phases):
            if p.name == name:
                return i
        return None

    def next_phase(self, name: str) -> str | None:
        idx = self.phase_index(name)
        if idx is None or idx + 1 >= len(self.phases):
            return None
        return self.phases[idx + 1].name

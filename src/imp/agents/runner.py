"""Internal message shapes + AgentRunner protocol (spec §6.1, §6.3, §6.4).

All three V1 runners (Anthropic this plan; OpenAI + Claude Code in Plan 2)
share these types. Each runner converts to/from its provider-native shape
inside `run()`. Transcripts on disk are JSONL of these types.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from imp.engine.types import AgentDef, FsRule


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: Literal["tool_use"] = "tool_use"


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False
    type: Literal["tool_result"] = "tool_result"

    def __post_init__(self) -> None:
        # The Claude Code SDK can hand us is_error=None (an explicit null, not a
        # missing attr). None serializes to JSON null, which the Anthropic API
        # rejects on replay. Coerce once here — the constructor is the single
        # boundary every persistence/replay path funnels through. frozen+slots
        # forbids normal assignment, so use object.__setattr__.
        object.__setattr__(self, "is_error", bool(self.is_error))


ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock


@dataclass(frozen=True, slots=True)
class Message:
    role: Literal["user", "assistant", "system"]
    content: tuple[ContentBlock, ...]


@dataclass(frozen=True, slots=True)
class Diagnostics:
    """Render-side extraction from a provider response: block warnings to
    surface as runner_warning events, and extended-thinking contents to
    surface as thinking events. Not consulted by reconstruction."""
    block_warnings: tuple[str, ...] = ()
    thinkings: tuple[str, ...] = ()


# Engine → runner boundary value. Frozen to make it safe to pass to subagents.
@dataclass(frozen=True, slots=True)
class ChangeContext:
    handle: str
    title: str
    project_root: Path
    workflow_dir: Path
    id: str = ""   # stable Change identity (spec §1). Defaulted so test
    # constructors stay green; production sites (executor, subagents) pass it.
    change_home: str = ""
    project_facts: dict[str, str] = field(hash=False, default_factory=dict)
    guidance: str = ""


@dataclass(frozen=True, slots=True)
class AgentInvocation:
    agent_def: AgentDef
    provider_name: str                     # "anthropic" | "openai" | "claude_code"
    model_name: str
    system_prompt: str
    initial_messages: tuple[Message, ...]
    change_context: ChangeContext
    transcript_dir: Path                   # .imp/changes/<id>/steps/<phase>/<step>/
    fs_permissions: tuple[FsRule, ...]
    events_path: Path
    subagent_defs: dict[str, AgentDef] = field(default_factory=dict)
    # resolved by engine (Task 27a/28); None means no tool-count cap (opt-in only)
    max_tool_calls: int | None = None
    # The wait_for_user / AWAITING_USER resume input channel. Wired for attended
    # top-level agents; None for subagents (their wait_for_user pauses headless).
    # May return either a `str` or an `Awaitable[str]`; call-sites await-if-awaitable.
    user_input_fn: Callable[[str], str | Awaitable[str]] | None = None
    # Set by the executor for the END-OF-TURN nudge — distinct from
    # user_input_fn, which serves the wait_for_user / AWAITING_USER resume path.
    # Called with the agent's last text; returns the next user-message text to
    # inject and continue, or None to stop (→ agent_incomplete). None means
    # "no end-of-turn nudge wired" (legacy/test); providers then emit the
    # immediate no_complete_step behavior.
    continuation_fn: (
        Callable[[str], str | None | Awaitable[str | None]] | None
    ) = None
    # Stable identity of this agent invocation (step/gate/subagent), used as
    # the strict filter key for reconstruction. Derived by the executor /
    # dispatch_task; empty string only in legacy direct-construction tests.
    agent_id: str = ""
    # Shared EventLog instance (imp.engine.state.EventLog protocol). The runner
    # writes audit events through this; None means "construct a private
    # JsonlEventLog over events_path" (legacy/test fallback).
    event_log: Any = None
    # Live engine State for the rename_change tool to mutate. Any to avoid an
    # import cycle. None for subagents / legacy tests.
    change_state: Any = None
    # The LIVE, mutable Change for the rename_change tool. Any to avoid an import
    # cycle. None for subagents / legacy tests (no rename authority anyway).
    change: Any = None
    # parent_id override for this agent's FIRST emitted event (step_context):
    # the spawning step_started id (steps/gates) or task tool_call id (subagents).
    first_event_parent_id: str | None = None
    # Set by the executor when this invocation targets a step that just had a
    # gate fail: the already-formatted feedback string to inject on resume
    # (design §4). None on every fresh run and every non-targeted step.
    retry_feedback: str | None = None
    # When True, a resumed transcript that ends in a successful complete_step is
    # balanced with a SUCCESS-shaped tool_result (the agent's accepted work is
    # superseded, not failed). Set by the executor for every re-entry driven by
    # is_resume (a `resume:` edge). retry_feedback (above) now means "optional
    # wrapper text appended on that balancing user turn"; None = balance only.
    balance_on_resume: bool = False
    # tier name → ProviderModel, the run's tier map. Threaded by the executor
    # into every AgentInvocation; copied onto ToolContext so dispatch_task can
    # place a subagent on its tier's resolved provider. Typed Any to avoid
    # importing ProviderModel here.
    resolved_tiers: dict[str, Any] | None = None
    # The effective, config tier menu for this run (ordered
    # cheapest->strongest). Copied onto ToolContext by the provider; surfaced to
    # the model as the `task` tool's optional `tier` enum. () = feature off.
    tiers: tuple[str, ...] = ()
    # Nesting bound's threading path: dispatch_task sets the child's depth to
    # parent + 1 and refuses past MAX_SUBAGENT_DEPTH. A field WITHOUT this
    # threading would read 0 forever and never trip. Each provider copies it
    # onto the child ToolContext (alongside resolved_tiers).
    subagent_depth: int = 0
    # The run's InterruptCoordinator. Copied onto ToolContext so dispatch_task
    # can drive a subagent with pause detection + interrupt attachment. Any to
    # avoid importing cli.signals here. None for legacy/test construction.
    coordinator: Any = None
    # config.verify_command, threaded onto the child ToolContext by each provider
    # so the agent bash tool can inject IMP_VERIFY_COMMAND. None => no verify gate.
    verify_command: str | None = None
    # §10/§12: when True, the run is headless (no human at the keyboard) and the
    # subagent backoff loop applies a wall-clock ceiling instead of waiting forever.
    # Threaded from run_change → _route_agent → _run_agent_invocation →
    # build_agent_invocation → provider → ToolContext → dispatch_task.
    non_interactive: bool = False
    # Resolved sandbox posture for this run. Threaded from run_change →
    # _route_agent → _run_agent_invocation → build_agent_invocation → provider →
    # ToolContext so every bash tool call applies the same posture. Any to avoid
    # importing SandboxPolicy here; None means no-op (bare sh -c, inactive policy).
    sandbox_policy: Any = None


Event = dict[str, Any]                     # {"type": str, "payload": dict}


class AgentRunner(Protocol):
    """Each provider's runner implements this. Engine consumes the event stream."""

    def run(self, invocation: AgentInvocation) -> AsyncGenerator[Event, None]:
        """Yield events. Terminal event is `agent_output` (success) or `error`."""
        ...

    async def interrupt(self) -> None:
        """Cancel any in-flight LLM stream or tool subprocess. Idempotent.
        Called by `cli/signals.py` on ESC/Ctrl+C/Ctrl+D."""
        ...


# ── JSON serialization for transcript.jsonl ────────────────────────────────


def message_to_json(msg: Message) -> str:
    payload: dict[str, Any] = {
        "role": msg.role,
        "content": [_block_to_dict(b) for b in msg.content],
    }
    return json.dumps(payload, separators=(",", ":"))


def _block_to_dict(b: ContentBlock) -> dict[str, Any]:
    if isinstance(b, TextBlock):
        return {"type": "text", "text": b.text}
    if isinstance(b, ToolUseBlock):
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    return {
        "type": "tool_result", "tool_use_id": b.tool_use_id,
        "content": b.content, "is_error": b.is_error,
    }


def message_from_json(data: dict[str, Any]) -> Message:
    blocks: list[ContentBlock] = []
    for raw in data["content"]:
        kind = raw["type"]
        if kind == "text":
            blocks.append(TextBlock(text=raw["text"]))
        elif kind == "tool_use":
            blocks.append(ToolUseBlock(id=raw["id"], name=raw["name"], input=raw["input"]))
        elif kind == "tool_result":
            blocks.append(ToolResultBlock(
                tool_use_id=raw["tool_use_id"],
                content=raw["content"],
                is_error=raw.get("is_error", False),
            ))
        else:
            raise ValueError(f"unknown block type {kind!r}")
    return Message(role=data["role"], content=tuple(blocks))

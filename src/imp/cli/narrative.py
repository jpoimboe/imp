# src/imp/cli/narrative.py
"""Narrative-tree model for imp's unified renderer.

Folds a stream of event dicts (events.jsonl shape) into an ordered tree of
typed nodes. Fed events ONE AT A TIME via NarrativeBuilder.add(), so the live
on_event path and the batch replay path (build_tree) share exactly one code
path and cannot drift (design §5.4).

PURITY (design D3): no module-level third-party imports. The one
provider-coupled operation — turning an llm_response.raw payload into assistant
text — is a LAZY import of imp.cli.render_helpers inside AgentTextNode.text,
exactly as today's render.py lazy-imports it. So `import narrative` is cheap and
tree-structure tests pull nothing heavy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── token accumulator (design §7.7 / F1: read usage straight from payload) ──
@dataclass
class TokenTotals:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_creation: int = 0

    def add_usage(self, usage: dict[str, Any] | None) -> None:
        if not usage:
            return
        self.input += int(usage.get("input_tokens", 0) or 0)
        self.output += int(usage.get("output_tokens", 0) or 0)
        self.cache_read += int(usage.get("cache_read_input_tokens", 0) or 0)
        self.cache_creation += int(usage.get("cache_creation_input_tokens", 0) or 0)


# ── nodes ───────────────────────────────────────────────────────────────────
# `collapsible` is a class attribute (not a dataclass field) so isinstance-free
# code can ask `node.collapsible`. The TUI memo keys on id(node), so nodes carry
# no explicit id field.

@dataclass
class WorkflowNode:
    change_id: int | None = None
    title: str = ""
    status: str = "running"            # running | paused | completed
    children: list = field(default_factory=list)
    collapsible = False


@dataclass
class PhaseNode:
    name: str
    children: list = field(default_factory=list)
    closed: bool = False
    collapsible = False


@dataclass
class StepNode:
    name: str                          # step name, or gate stem for agent gates
    kind: str | None = None            # None=normal step; "agent_gate"=gate container
    interactive: bool = False
    model: str | None = None
    provider: str | None = None
    children: list = field(default_factory=list)
    outcome: "OutcomeLine | None" = None
    closed: bool = False
    collapsible = False


@dataclass
class AgentTextNode:
    payload: dict                      # the llm_response payload
    _cache: Any = field(default=None, repr=False)  # cached RenderedMessage (F5)
    collapsible = False

    @property
    def rendered(self) -> Any:
        """Lazily parse the provider raw payload into a RenderedMessage.

        This is the ONE place narrative.py touches the provider parse chain
        (design D3). Import is lazy so `import narrative` stays cheap and only
        callers that render agent prose pull pydantic/httpx. Cached because the
        node is immutable (design §7.2) — a resize must not re-parse.
        """
        if self._cache is None:
            from imp.cli.render_helpers import render_llm_response
            self._cache = render_llm_response(self.payload)
        return self._cache

    @property
    def text(self) -> str:
        return "\n\n".join(t.strip() for t in self.rendered.texts if t.strip())


@dataclass
class UserMessageNode:
    text: str
    collapsible = False


@dataclass
class ThinkingFold:
    content: str
    collapsible = True


@dataclass
class ToolCall:
    call_id: str
    name: str
    input: dict
    result: str | None = None
    is_error: bool = False
    killed: bool = False


@dataclass
class ToolBurstFold:
    calls: list = field(default_factory=list)   # list[ToolCall]
    collapsible = True


@dataclass
class SubagentFold:
    subagent_name: str
    event_id: str                      # parent `task` tool_call EVENT id; routing
                                       # key is "sub:" + event_id
    block_id: str                      # tool_use block id; close-matching key
    nested: "NarrativeBuilder"
    event_count: int = 0
    running: bool = True
    outcome: str | None = None         # result summary when task tool_result lands
    is_error: bool = False
    collapsible = True

    @property
    def tree(self) -> "NarrativeTree":
        return self.nested.tree


@dataclass
class GateNode:
    kind: str                          # human | script | stub
    prompt: str | None = None
    options: list = field(default_factory=list)
    action: Any = None                 # dict {kind, target?}
    context: dict | None = None
    collapsible = False


@dataclass
class ErrorNode:
    message: str
    category: str | None = None
    is_warning: bool = False
    collapsible = False


@dataclass
class StepContextNode:
    payload: dict
    collapsible = True


@dataclass
class OutcomeLine:
    action: Any                        # str (step_completed) | dict (gate_evaluated)
    collapsible = False


@dataclass
class FallbackLine:
    event_type: str
    payload: dict
    collapsible = False


@dataclass
class NarrativeTree:
    root: WorkflowNode
    tokens: TokenTotals

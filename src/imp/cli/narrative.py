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


# ── builder ──────────────────────────────────────────────────────────────────
from collections.abc import Iterable   # noqa: E402  (kept near builder for clarity)


class NarrativeBuilder:
    """Per-agent_id state machine. add(event) routes one event into the tree.

    Routing order in add() (design §5.3):
      0. accumulate token usage for EVERY llm_response (before sub routing, so
         subagent tokens count toward the change total — F2).
      1. if agent_id is a registered child subagent fold → delegate down.
      2. else dispatch by event type at the current open scope.
    """

    def __init__(self) -> None:
        self._root = WorkflowNode()
        self._tokens = TokenTotals()
        self._current_phase: PhaseNode | None = None
        # Stack of open StepNode containers. A normal step pushes one; an agent
        # gate that runs *inside* a still-open step pushes a second (design
        # §5.3 — nested agent-gate container). step_completed / gate_evaluated
        # (kind=agent) pop.
        self._step_stack: list[StepNode] = []
        self._current_burst: ToolBurstFold | None = None
        # subagent routing: "sub:<event_id>" -> fold (for delegating sub events)
        self._sub_by_agent_id: dict[str, SubagentFold] = {}
        # open subagent folds keyed by tool_use block id (to close on the
        # parent's `task` tool_result, which carries the block id, not event id)
        self._open_sub_by_block: dict[str, SubagentFold] = {}
        self._pending_human_gate: GateNode | None = None
        # True for the window between a workflow_resumed event and the first
        # re-emitted step_started, so resume re-entry can reuse nodes instead of
        # duplicating them (see Task 10B). Default False; goto loops never set it.
        self._resuming = False

    # -- public API -----------------------------------------------------------
    @property
    def tree(self) -> NarrativeTree:
        return NarrativeTree(root=self._root, tokens=self._tokens)

    def add(self, event: Any) -> None:
        if not isinstance(event, dict) or "type" not in event:
            self._append(ErrorNode(message=_malformed_text(event), is_warning=False))
            return
        payload = event.get("payload")
        if not isinstance(payload, dict):
            self._append(ErrorNode(
                message=f"{event.get('type')}: malformed payload", is_warning=False))
            return

        etype = event["type"]
        agent_id = event.get("agent_id")

        # 0. token accumulation — every llm_response, before sub routing (F2).
        if etype == "llm_response":
            self._tokens.add_usage(payload.get("usage"))

        # 1. subagent routing by EXACT membership (not a "sub:" prefix check —
        #    a nested builder's own events carry a sub: id that is NOT one of
        #    its registered children; only delegate ids we actually registered).
        if isinstance(agent_id, str) and agent_id in self._sub_by_agent_id:
            fold = self._sub_by_agent_id[agent_id]
            fold.nested.add(event)
            fold.event_count += 1
            return

        # 2. dispatch by type.
        handler = self._HANDLERS.get(etype)
        if handler is None:
            self._append(FallbackLine(event_type=etype, payload=payload))
            return
        handler(self, event, payload)

    # -- scope helpers --------------------------------------------------------
    def _container(self):
        if self._step_stack:
            return self._step_stack[-1]
        if self._current_phase is not None:
            return self._current_phase
        return self._root

    def _append(self, node: Any) -> None:
        self._container().children.append(node)

    def _flush_burst(self) -> None:
        self._current_burst = None

    # -- per-type handlers are registered in Task 3+ via _HANDLERS ------------
    _HANDLERS: dict = {}


def _malformed_text(event: Any) -> str:
    if isinstance(event, dict):
        return f"malformed event (missing type): {event!r}"[:200]
    return f"malformed event: {event!r}"[:200]


def build_tree(events: Iterable[dict]) -> NarrativeTree:
    b = NarrativeBuilder()
    for e in events:
        b.add(e)
    return b.tree


# Handlers populate this in subsequent tasks; change_created lives here so the
# skeleton test passes.
def _h_change_created(self: NarrativeBuilder, event: dict, p: dict) -> None:
    self._root.change_id = p.get("change_id")
    self._root.title = p.get("title", "")


NarrativeBuilder._HANDLERS["change_created"] = _h_change_created


def _h_phase_started(self: NarrativeBuilder, event: dict, p: dict) -> None:
    self._flush_burst()
    phase = PhaseNode(name=p.get("phase", "?"))
    self._root.children.append(phase)
    self._current_phase = phase
    self._step_stack.clear()


def _h_phase_completed(self: NarrativeBuilder, event: dict, p: dict) -> None:
    self._flush_burst()
    self._step_stack.clear()
    if self._current_phase is not None:
        self._current_phase.closed = True


def _h_step_started(self: NarrativeBuilder, event: dict, p: dict) -> None:
    self._flush_burst()
    if p.get("kind") == "agent_gate":
        node = StepNode(
            name=p.get("gate_stem", "?"), kind="agent_gate",
            model=p.get("model"), provider=p.get("provider"),
            interactive=bool(p.get("interactive", False)))
    else:
        node = StepNode(
            name=p.get("step", "?"), kind=None,
            model=p.get("model"), provider=p.get("provider"),
            interactive=bool(p.get("interactive", False)))
    # Attach to whatever is open: a still-open step (nested agent gate) → that
    # step's children; else the current phase; else root (defensive / nested
    # subagent trees that never see a phase).
    self._append(node)
    self._step_stack.append(node)


def _h_step_completed(self: NarrativeBuilder, event: dict, p: dict) -> None:
    self._flush_burst()
    outcome = OutcomeLine(action=p.get("action"))
    node = self._pop_step(p.get("step"))
    if node is not None:
        node.outcome = outcome
        node.children.append(outcome)
        node.closed = True


def _pop_step(self: NarrativeBuilder, name: str | None) -> StepNode | None:
    """Pop the top open step. The top is always the right target: a normal step
    is alone on the stack, and an agent-gate container is pushed *above* its
    enclosing step, so its closing gate_evaluated pops the gate, not the step.
    `name` is accepted for symmetry with the event payload but intentionally
    unused (closing is best-effort — design §5.1)."""
    if not self._step_stack:
        return None
    return self._step_stack.pop()


NarrativeBuilder._pop_step = _pop_step  # type: ignore[attr-defined]
NarrativeBuilder._HANDLERS["phase_started"] = _h_phase_started
NarrativeBuilder._HANDLERS["phase_completed"] = _h_phase_completed
NarrativeBuilder._HANDLERS["step_started"] = _h_step_started
NarrativeBuilder._HANDLERS["step_completed"] = _h_step_completed


def _h_workflow_paused(self: NarrativeBuilder, event: dict, p: dict) -> None:
    self._flush_burst()
    self._root.status = "paused"


def _h_workflow_resumed(self: NarrativeBuilder, event: dict, p: dict) -> None:
    self._flush_burst()
    self._root.status = "running"


NarrativeBuilder._HANDLERS["workflow_paused"] = _h_workflow_paused
NarrativeBuilder._HANDLERS["workflow_resumed"] = _h_workflow_resumed


def _h_human_gate_pending(self: NarrativeBuilder, event: dict, p: dict) -> None:
    self._flush_burst()
    gate = GateNode(kind="human", prompt=str(p.get("prompt", "")),
                    options=list(p.get("options") or []))
    self._append(gate)
    self._pending_human_gate = gate


def _h_gate_evaluated(self: NarrativeBuilder, event: dict, p: dict) -> None:
    self._flush_burst()
    kind = str(p.get("kind", "?"))
    action = p.get("action")
    if kind == "agent":
        # Closes the open agent-gate container StepNode (opened by its
        # step_started kind=agent_gate — Task 3).
        outcome = OutcomeLine(action=action)
        node = self._pop_step(None)
        if node is not None:
            node.outcome = outcome
            node.children.append(outcome)
            node.closed = True
        return
    if kind == "human" and self._pending_human_gate is not None:
        self._pending_human_gate.action = action
        self._pending_human_gate = None
        return
    self._append(GateNode(kind=kind, action=action, context=p.get("context")))


NarrativeBuilder._HANDLERS["human_gate_pending"] = _h_human_gate_pending
NarrativeBuilder._HANDLERS["gate_evaluated"] = _h_gate_evaluated


def _user_message_text(payload: dict) -> str:
    """Inline (pure) extraction of user chat text. Deliberately NOT
    render_helpers.user_message_text — importing that module pulls the provider
    parse chain at module load (D3). This logic is trivial; keep it local."""
    return "".join(
        b.get("text", "") for b in (payload.get("content") or [])
        if isinstance(b, dict) and b.get("type") == "text")


def _h_user_message(self: NarrativeBuilder, event: dict, p: dict) -> None:
    self._flush_burst()
    self._append(UserMessageNode(text=_user_message_text(p)))


def _h_llm_response(self: NarrativeBuilder, event: dict, p: dict) -> None:
    # Token accumulation already happened in add(); here we only build the node.
    self._flush_burst()
    self._append(AgentTextNode(payload=p))


def _h_thinking(self: NarrativeBuilder, event: dict, p: dict) -> None:
    self._flush_burst()
    self._append(ThinkingFold(content=str(p.get("content", ""))))


def _h_step_context(self: NarrativeBuilder, event: dict, p: dict) -> None:
    # step_context is the agent's first event; do NOT flush — it opens, not
    # interrupts, the content stream. (No burst can be open before it anyway.)
    self._append(StepContextNode(payload=p))


def _h_agent_output(self: NarrativeBuilder, event: dict, p: dict) -> None:
    # design §5.1: agent_output is the executor's step-result capture consumed
    # by templating; it has NO node. The visible outcome is the OutcomeLine.
    return


NarrativeBuilder._HANDLERS["user_message"] = _h_user_message
NarrativeBuilder._HANDLERS["llm_response"] = _h_llm_response
NarrativeBuilder._HANDLERS["thinking"] = _h_thinking
NarrativeBuilder._HANDLERS["step_context"] = _h_step_context
NarrativeBuilder._HANDLERS["agent_output"] = _h_agent_output


def _ensure_burst(self: NarrativeBuilder) -> ToolBurstFold:
    if self._current_burst is None:
        self._current_burst = ToolBurstFold()
        self._append(self._current_burst)
    return self._current_burst


def _h_tool_call(self: NarrativeBuilder, event: dict, p: dict) -> None:
    if p.get("name") == "task":
        _open_subagent_fold(self, event, p)     # defined in Task 6
        return
    burst = _ensure_burst(self)
    burst.calls.append(ToolCall(
        call_id=str(p.get("id", "")), name=str(p.get("name", "tool")),
        input=p.get("input") or {}))


def _h_tool_result(self: NarrativeBuilder, event: dict, p: dict) -> None:
    rid = str(p.get("id", ""))
    # Closing a subagent? (parent `task` tool_result carries the block id.)
    fold = self._open_sub_by_block.pop(rid, None)
    if fold is not None:
        fold.running = False
        fold.is_error = bool(p.get("error", False))
        fold.outcome = _summarize_result(p.get("content"))
        return
    # Normal burst result: pair with the matching open call.
    burst = self._current_burst
    if burst is not None:
        for call in burst.calls:
            if call.call_id == rid and call.result is None:
                call.result = _summarize_result(p.get("content"))
                call.is_error = bool(p.get("error", False))
                return
    # Orphan result (malformed log): start/extend a burst with a "tool" entry.
    burst = _ensure_burst(self)
    burst.calls.append(ToolCall(
        call_id=rid, name="tool", input={},
        result=_summarize_result(p.get("content")),
        is_error=bool(p.get("error", False))))


def _h_tool_killed(self: NarrativeBuilder, event: dict, p: dict) -> None:
    rid = str(p.get("id", ""))
    burst = self._current_burst
    if burst is not None:
        for call in burst.calls:
            if call.call_id == rid:
                call.killed = True
                return
    # No matching open burst call → record a standalone killed marker.
    b = _ensure_burst(self)
    b.calls.append(ToolCall(
        call_id=rid, name=str(p.get("name", "tool")), input={}, killed=True))


def _summarize_result(content: Any) -> str:
    """Full underlying result text, newlines preserved. The view decides how to
    wrap/elide; the model keeps the whole thing (the 'full messages' goal)."""
    if content is None:
        return ""
    return content if isinstance(content, str) else str(content)


NarrativeBuilder._HANDLERS["tool_call"] = _h_tool_call
NarrativeBuilder._HANDLERS["tool_result"] = _h_tool_result
NarrativeBuilder._HANDLERS["tool_killed"] = _h_tool_killed


def _open_subagent_fold(self: NarrativeBuilder, event: dict, p: dict) -> None:
    """A `task` tool_call opens a SubagentFold (never part of a burst — §5.3
    rule 2). Routing key is "sub:" + the tool_call EVENT id; the closing
    `task` tool_result is matched by the tool_use BLOCK id (payload['id']).

    NOTE: routing is SINGLE-LEVEL. A grandchild's `sub:<id2>` events are not
    delegated into this fold's nested builder (the top builder only knows its
    own registered child ids). This is currently safe because spawned subagents
    receive `subagent_defs={}` (subagents.py:104) and `dispatch_task` rejects
    undeclared names (subagents.py:57-61), so subagents cannot spawn subagents.
    Spec §5.2 ("an expanded subagent is itself a narrative with its own … sub
    folds") implies nesting; if the engine ever enables it, routing must become
    recursive/prefix-aware and a nested-subagent fixture added."""
    self._flush_burst()
    event_id = str(event.get("id", ""))
    block_id = str(p.get("id", ""))
    inp = p.get("input") or {}
    fold = SubagentFold(
        subagent_name=str(inp.get("subagent_name", "?")),
        event_id=event_id, block_id=block_id, nested=NarrativeBuilder())
    self._append(fold)
    self._sub_by_agent_id[f"sub:{event_id}"] = fold
    self._open_sub_by_block[block_id] = fold

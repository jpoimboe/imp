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

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar


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
    children: list[Any] = field(default_factory=list)
    collapsible = False


@dataclass
class PhaseNode:
    name: str
    children: list[Any] = field(default_factory=list)
    closed: bool = False
    collapsible = False


@dataclass
class StepNode:
    name: str                          # step name
    kind: str | None = None            # agent | script | human | branch
    interactive: bool = False
    model: str | None = None
    provider: str | None = None
    children: list[Any] = field(default_factory=list)
    outcome: OutcomeLine | None = None
    start_ts: str | None = None        # ISO-8601 from step_started.timestamp (issue 3)
    agent_output: dict[str, Any] | None = None  # stashed complete_step args (issues 5/6)
    closed: bool = False
    collapsible = False


@dataclass
class AgentTextNode:
    payload: dict[str, Any]            # the llm_response payload
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
    input: dict[str, Any]
    result: str | None = None
    is_error: bool = False
    killed: bool = False


@dataclass
class ToolBurstFold:
    calls: list[ToolCall] = field(default_factory=list)
    collapsible = True


@dataclass
class SubagentFold:
    subagent_name: str
    event_id: str                      # parent `task` tool_call EVENT id; routing
                                       # key is "sub:" + event_id
    block_id: str                      # tool_use block id; close-matching key
    nested: NarrativeBuilder
    event_count: int = 0
    running: bool = True
    outcome: str | None = None         # result summary when task tool_result lands
    is_error: bool = False
    collapsible = True

    @property
    def tree(self) -> NarrativeTree:
        return self.nested.tree

    def current_activity(self) -> str:
        detail = self.nested.current_activity()
        tail = f" · {detail}" if detail else ""
        return f"task → {self.subagent_name} · {self.event_count} events{tail}"


@dataclass
class GateNode:
    kind: str                          # human | script | stub
    prompt: str | None = None
    options: list[Any] = field(default_factory=list)
    action: Any = None                 # dict {kind, target?}
    context: dict[str, Any] | None = None
    collapsible = False


@dataclass
class ErrorNode:
    message: str
    category: str | None = None
    is_warning: bool = False
    collapsible = False


@dataclass
class StepContextNode:
    payload: dict[str, Any]
    collapsible = True


@dataclass
class OutcomeLine:
    action: Any                        # dict {verb, target?} from step_completed
    collapsible = False


@dataclass
class FallbackLine:
    event_type: str
    payload: dict[str, Any]
    collapsible = False


@dataclass
class NarrativeTree:
    root: WorkflowNode
    tokens: TokenTotals


# ── builder ──────────────────────────────────────────────────────────────────
from collections.abc import Iterable  # noqa: E402  (kept near builder for clarity)


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
        # Stack of open StepNode containers. step_started pushes one;
        # step_completed pops it. Every kind (agent/script/human/branch) is a
        # normal step — there is no nested gate container.
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

    @property
    def current_burst(self) -> ToolBurstFold | None:
        """The open (still-accumulating) tool burst, or None. Public read-only
        accessor for the live driver's finalization check and LiveStatus."""
        return self._current_burst

    def current_activity(self) -> str | None:
        """A one-line description of what the active step is doing right now, for
        the live status line. None when no step is active. Reads only public-ish
        state via this method so LiveStatus never touches builder internals."""
        step = (self._step_stack[-1].name if self._step_stack
                else self._current_phase.name if self._current_phase else None)
        for fold in self._open_sub_by_block.values():
            if fold.running:
                return fold.current_activity()
        burst = self._current_burst
        if burst is not None and burst.calls:
            last = burst.calls[-1]
            arg = next(iter((last.input or {}).values()), "")
            head = f"{step} · " if step else ""
            return f"{head}{last.name} {arg} · {len(burst.calls)} tools".rstrip()
        if step is not None:
            return f"{step} · working"
        return None

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
    def _container(self) -> WorkflowNode | PhaseNode | StepNode:
        if self._step_stack:
            return self._step_stack[-1]
        if self._current_phase is not None:
            return self._current_phase
        return self._root

    def _append(self, node: Any) -> None:
        self._container().children.append(node)

    def _flush_burst(self) -> None:
        self._current_burst = None

    def _pop_step(self, name: str | None) -> StepNode | None:
        """Pop the top open step. `name` is accepted for symmetry with the event
        payload but intentionally unused (closing is best-effort — design §5.1)."""
        if not self._step_stack:
            return None
        return self._step_stack.pop()

    # -- per-type handlers are registered in Task 3+ via _HANDLERS ------------
    _HANDLERS: ClassVar[
        dict[str, Callable[[NarrativeBuilder, dict[str, Any], dict[str, Any]], None]]
    ] = {}


def _malformed_text(event: Any) -> str:
    if isinstance(event, dict):
        return f"malformed event (missing type): {event!r}"[:200]
    return f"malformed event: {event!r}"[:200]


def build_tree(events: Iterable[dict[str, Any]]) -> NarrativeTree:
    b = NarrativeBuilder()
    for e in events:
        b.add(e)
    return b.tree


# Handlers populate this in subsequent tasks; change_created lives here so the
# skeleton test passes.
def _h_change_created(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._root.change_id = p.get("change_id")
    self._root.title = p.get("title", "")


NarrativeBuilder._HANDLERS["change_created"] = _h_change_created


def _ensure_phase(self: NarrativeBuilder, name: str) -> None:
    """Derive the phase grouping from step_started.payload.phase. When the phase
    name changes from the current open one, lazily create a PhaseNode, append it
    to the workflow root, and make it the current scope. There are no
    phase_started/phase_completed events — phase is purely DERIVED here."""
    if (self._current_phase is not None and not self._current_phase.closed
            and self._current_phase.name == name):
        return
    phase = PhaseNode(name=name)
    self._root.children.append(phase)
    self._current_phase = phase
    self._step_stack.clear()


def _h_step_started(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_burst()
    name = p.get("step", "?")
    kind = p.get("kind")
    phase = p.get("phase", "?")
    # Resume re-entry: the first step_started after a resume re-enters the
    # interrupted step in the still-open phase. The executor re-runs it from
    # scratch, so reuse the open StepNode and RESET its content (the fresh re-run
    # supersedes the interrupted partial). Consume the re-entry window regardless
    # of whether a match is found.
    resuming, self._resuming = self._resuming, False
    if (resuming and self._current_phase is not None
            and not self._current_phase.closed
            and self._current_phase.name == phase):
        existing = next(
            (c for c in self._current_phase.children
             if isinstance(c, StepNode) and c.name == name and not c.closed), None)
        if existing is not None:
            existing.children.clear()
            existing.outcome = None
            existing.closed = False
            existing.kind = kind
            existing.model = p.get("model")
            existing.provider = p.get("provider")
            existing.interactive = bool(p.get("interactive", False))
            existing.start_ts = event.get("timestamp")
            existing.agent_output = None
            self._step_stack = [existing]
            return
    _ensure_phase(self, phase)
    node = StepNode(name=name, kind=kind, model=p.get("model"),
                    provider=p.get("provider"),
                    interactive=bool(p.get("interactive", False)),
                    start_ts=event.get("timestamp"))
    self._append(node)
    self._step_stack.append(node)


def _h_step_completed(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_burst()
    action = p.get("action")          # dict {verb, target?}; var_mutations is a sibling
    # A human step's resolved route arrives here (no separate gate_evaluated). If
    # this step opened a human prompt, stamp its resolved action onto that gate.
    if self._pending_human_gate is not None:
        self._pending_human_gate.action = action
        self._pending_human_gate = None
    outcome = OutcomeLine(action=action)
    node = self._pop_step(p.get("step"))
    if node is not None:
        node.outcome = outcome
        node.children.append(outcome)
        node.closed = True


NarrativeBuilder._HANDLERS["step_started"] = _h_step_started
NarrativeBuilder._HANDLERS["step_completed"] = _h_step_completed


def _h_workflow_paused(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_burst()
    self._root.status = "paused"
    reason = p.get("reason")
    if reason:
        kind = p.get("kind")
        suffix = f" ({kind})" if kind else ""
        self._append(ErrorNode(
            message=f"paused: {reason}{suffix}", category=reason, is_warning=False))


def _h_workflow_resumed(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_burst()
    self._root.status = "running"
    self._resuming = True                # open the re-entry window


NarrativeBuilder._HANDLERS["workflow_paused"] = _h_workflow_paused
NarrativeBuilder._HANDLERS["workflow_resumed"] = _h_workflow_resumed


def _h_human_prompt_pending(
    self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any],
) -> None:
    # A human step already emitted step_started (kind=human), so _h_step_started
    # opened its phase + StepNode. This event only ANNOTATES that open step with
    # the prompt/options — it does NOT open a second node. The resolved route
    # arrives via the step's step_completed.action (a dict), which both closes
    # the StepNode and stamps the gate (see _h_step_completed).
    self._flush_burst()
    gate = GateNode(kind="human", prompt=str(p.get("ask", "")),
                    options=list(p.get("options") or []))
    self._append(gate)
    self._pending_human_gate = gate


def _h_awaiting_user_input(
    self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any],
) -> None:
    # Programmatic marker that the engine is blocking for an interactive
    # end-of-turn reply. The question already rendered as agent text and the
    # reply renders as the next user_message, so render nothing here — this
    # exists for out-of-process drivers, not the human narrative.
    return None


NarrativeBuilder._HANDLERS["human_prompt_pending"] = _h_human_prompt_pending
NarrativeBuilder._HANDLERS["awaiting_user_input"] = _h_awaiting_user_input


def _user_message_text(payload: dict[str, Any]) -> str:
    """Inline (pure) extraction of user chat text. Deliberately NOT
    render_helpers.user_message_text — importing that module pulls the provider
    parse chain at module load (D3). This logic is trivial; keep it local."""
    return "".join(
        b.get("text", "") for b in (payload.get("content") or [])
        if isinstance(b, dict) and b.get("type") == "text")


def _h_user_message(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_burst()
    self._append(UserMessageNode(text=_user_message_text(p)))


def _h_llm_response(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # Token accumulation already happened in add(); here we only build the node.
    self._flush_burst()
    self._append(AgentTextNode(payload=p))


def _h_thinking(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_burst()
    self._append(ThinkingFold(content=str(p.get("content", ""))))


def _h_step_context(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # step_context is the agent's first event; do NOT flush — it opens, not
    # interrupts, the content stream. (No burst can be open before it anyway.)
    self._append(StepContextNode(payload=p))


def _h_agent_output(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
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


def _h_tool_call(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    if p.get("name") == "task":
        _open_subagent_fold(self, event, p)     # defined in Task 6
        return
    burst = _ensure_burst(self)
    burst.calls.append(ToolCall(
        call_id=str(p.get("id", "")), name=str(p.get("name", "tool")),
        input=p.get("input") or {}))


def _h_tool_result(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
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


def _h_tool_killed(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
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


def _flatten_block(b: Any) -> str:
    # Read the block type with b["type"] / b.get("type") — `type` is the Python
    # builtin; do not use a bare `type` name here.
    if isinstance(b, dict):
        if b.get("type") == "text":
            # `or ""` hardens against a malformed {"type":"text","text":null}
            # block: returning None here would make the "".join() below raise.
            # Real text blocks always carry a str; this just preserves the old
            # str() fallback's never-raise property.
            return b.get("text") or ""
        if "type" in b:
            return f"[{b['type']}]"
        return str(b)        # dict with no type: unknown shape, preserve info
    return str(b)            # non-dict element: preserve info


def _summarize_result(content: Any) -> str:
    """Full underlying result text, newlines preserved. The view decides how to
    wrap/elide; the node keeps the whole thing (the 'full messages' goal)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Flatten a provider content-block list. Join with "" (no separator),
        # matching the text extractors but as a superset: non-text blocks are
        # surfaced as [<type>] tags rather than silently dropped.
        return "".join(_flatten_block(b) for b in content)
    return str(content)      # safety net for any other shape (unchanged)


NarrativeBuilder._HANDLERS["tool_call"] = _h_tool_call
NarrativeBuilder._HANDLERS["tool_result"] = _h_tool_result
NarrativeBuilder._HANDLERS["tool_killed"] = _h_tool_killed


def _h_error(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_burst()
    self._append(ErrorNode(
        message=str(p.get("message", "?")), category=p.get("category"),
        is_warning=False))


def _h_runner_warning(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # Non-terminal: do NOT flush the burst (a warning can be emitted mid-burst
    # for a dropped block; flushing would split an otherwise-contiguous run).
    self._append(ErrorNode(message=str(p.get("message", "?")), is_warning=True))


def _h_agent_retry(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # Non-terminal: a transient error was retried with backoff. Render inline so
    # a self-healed network/rate_limit blip doesn't look like a failure.
    cat = p.get("category", "?")
    attempt = p.get("attempt", "?")
    secs = p.get("backoff_seconds", "?")
    self._append(ErrorNode(
        message=f"[{cat}] retry {attempt} ({secs}s)", category=cat, is_warning=True))


NarrativeBuilder._HANDLERS["error"] = _h_error
NarrativeBuilder._HANDLERS["runner_warning"] = _h_runner_warning
NarrativeBuilder._HANDLERS["agent_retry"] = _h_agent_retry


def _open_subagent_fold(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
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


def structural_repr(node: Any) -> Any:
    """A JSON-able structural snapshot of a node subtree, for parity asserts.

    Captures node type, structural fields, and children recursively. Excludes
    only the lazy RenderedMessage cache (an identity/perf detail). Used by the
    §5.4 incremental==batch test and by future regression tests."""
    t = type(node).__name__
    if isinstance(node, WorkflowNode):
        return {"t": t, "change_id": node.change_id, "title": node.title,
                "status": node.status, "children": [structural_repr(c) for c in node.children]}
    if isinstance(node, PhaseNode):
        return {"t": t, "name": node.name, "closed": node.closed,
                "children": [structural_repr(c) for c in node.children]}
    if isinstance(node, StepNode):
        return {"t": t, "name": node.name, "kind": node.kind,
                "interactive": node.interactive, "closed": node.closed,
                "outcome": structural_repr(node.outcome) if node.outcome else None,
                "children": [structural_repr(c) for c in node.children]}
    if isinstance(node, AgentTextNode):
        return {"t": t, "text": node.text}
    if isinstance(node, UserMessageNode):
        return {"t": t, "text": node.text}
    if isinstance(node, ThinkingFold):
        return {"t": t, "content": node.content}
    if isinstance(node, ToolBurstFold):
        return {"t": t, "calls": [
            {"name": c.name, "call_id": c.call_id, "result": c.result,
             "is_error": c.is_error, "killed": c.killed} for c in node.calls]}
    if isinstance(node, SubagentFold):
        return {"t": t, "subagent_name": node.subagent_name, "event_id": node.event_id,
                "block_id": node.block_id, "running": node.running,
                "outcome": node.outcome, "is_error": node.is_error,
                "event_count": node.event_count,
                "nested": [structural_repr(c) for c in node.tree.root.children]}
    if isinstance(node, GateNode):
        return {"t": t, "kind": node.kind, "prompt": node.prompt,
                "options": node.options, "action": node.action, "context": node.context}
    if isinstance(node, ErrorNode):
        return {"t": t, "message": node.message, "category": node.category,
                "is_warning": node.is_warning}
    if isinstance(node, StepContextNode):
        return {"t": t, "payload": node.payload}
    if isinstance(node, OutcomeLine):
        return {"t": t, "action": node.action}
    if isinstance(node, FallbackLine):
        return {"t": t, "event_type": node.event_type, "payload": node.payload}
    return {"t": t}

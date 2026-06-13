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

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

# Tool names omitted from the burst recap AND the live status basis (§1/§3). They
# render elsewhere (load_skill → its own `load skill …` line) or are placeholders
# (`tool`), and `wait_for_user` becomes its own synthesized user turn (§1).
_OMIT: frozenset[str] = frozenset({"load_skill", "tool", "wait_for_user"})

# Chrome step kinds (§4 status precedence). A frozenset so the constant cannot drift.
CHROME_KINDS: frozenset[str] = frozenset({"script", "branch", "merge"})


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

_PROSE_THRESHOLD = 80  # issues 5/6: string output fields longer than this (or
                       # multi-line) render as a bold prose block; shorter scalars
                       # and *_path fields fold into the breadcrumb.

@dataclass
class WorkflowNode:
    slug: str | None = None
    title: str = ""
    change_id: str | None = None
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
    kind: str | None = None            # agent | script | branch | merge
    interactive: bool = False
    model: str | None = None
    provider: str | None = None
    children: list[Any] = field(default_factory=list)
    outcome: OutcomeLine | None = None
    start_ts: str | None = None        # ISO-8601 from step_started.timestamp (issue 3)
    end_ts: str | None = None          # ISO-8601 from step_completed.timestamp (§7)
    agent_output: dict[str, Any] | None = None  # stashed complete_step args (issues 5/6)
    quiet: bool = False                # the step_started.payload.quiet flag
    quiet_line: str | None = None      # resolved routing line (`→ reworking` /
                                       # `→ <phase>`); None for a linear continue
    closed: bool = False
    interrupted: bool = False
    collapsible = False


@dataclass
class AgentTextNode:
    payload: dict[str, Any]            # the llm_response payload
    _cache: Any = field(default=None, repr=False)  # cached RenderedMessage (F5)
    held: bool = False  # True until agent_output, step_completed, or llm_response resolves it
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
    interactive: bool = False           # True only for a genuine human reply (one
                                        # the engine blocked for via awaiting_user_input).
                                        # Synthetic prompts — the "begin" kickoff, a
                                        # resume nudge — are False and never rendered.
    pending_question: str = ""          # the prompt carried by the awaiting_user_input
                                        # event that solicited this reply (M3). Rendered
                                        # as the question line above the `>` reply caret.
                                        # Empty when the solicit carried no prompt.
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
    call_ts: str | None = None         # the constructing event's timestamp (§1 burst clock)
    citations_footer: str = ""          # §2: rendered as the block's final line, outside the clip


@dataclass
class ToolBurstFold:
    calls: list[ToolCall] = field(default_factory=list)
    start_ts: str | None = None        # first tool_call.timestamp (burst timing)
    end_ts: str | None = None          # last tool_result.timestamp
    collapsible = True


def visible_calls(burst: ToolBurstFold) -> list[ToolCall]:
    """Burst calls minus the _OMIT names — the shared basis for the lean recap
    (render_text) and the live status count/named-call (current_activity)."""
    return [c for c in burst.calls if c.name not in _OMIT]


def display_tool_name(name: str) -> str:
    return name.rsplit("__", 1)[-1] if name.startswith("mcp__") else name


@dataclass
class Activity:
    """The structured live-status description (§3): count phrase, gray detail
    (tool name + arg), and a fallback label. Any field may be None. Plain text —
    the driver colors the segments (live_driver.compose_status)."""
    count: str | None = None
    detail: str | None = None
    label: str | None = None


def activity_to_text(activity: Activity | None) -> str:
    """Flatten an Activity to one plain (no-ANSI) line — used by SubagentFold to
    fold its nested builder's activity into its own status text."""
    if activity is None:
        return ""
    return " · ".join(p for p in (activity.count, activity.detail, activity.label) if p)


@dataclass
class SkillLoadNode:
    """A skill the agent pulled in mid-run (the skill_load event). Rendered as a
    quiet `load skill {name}` line — distinct from the redundant `load_skill`
    tool call, which the lean burst recap drops so the skill shows exactly once."""
    name: str
    source: str | None = None
    collapsible = False


@dataclass
class SubagentFold:
    subagent_name: str
    event_id: str                      # parent `task` tool_call EVENT id; routing
                                       # key is "sub:" + event_id
    block_id: str                      # tool_use block id; close-matching key
    nested: NarrativeBuilder
    opener_agent_id: str | None = None  # agent_id that emitted the `task` tool_call
                                        # (the parent). When that same parent emits
                                        # its NEXT event, control has returned from
                                        # the synchronous `await dispatch_task`, so a
                                        # still-open fold is resume-closed (fallback
                                        # for a missing/lost task tool_result).
    event_count: int = 0
    running: bool = True
    outcome: str | None = None         # result summary when task tool_result lands
    is_error: bool = False
    terminal_state: str | None = None  # 'stalled' (live via B.2); 'interrupted'
                                       # is forward-compat scaffolding; else None
    retry_attempt: int = 0
    start_ts: str | None = None        # parent `task` tool_call.timestamp
    end_ts: str | None = None          # parent `task` tool_result.timestamp
    collapsible = True

    @property
    def tree(self) -> NarrativeTree:
        return self.nested.tree

    def current_activity(self) -> str:
        detail = activity_to_text(self.nested.current_activity())
        tail = f" · {detail}" if detail else ""
        return f"task → {self.subagent_name} · {self.event_count} events{tail}"


@dataclass
class ErrorNode:
    message: str
    category: str | None = None
    is_warning: bool = False
    detail: str = ""                   # §6: full_summary for the --full detail block
    collapsible = False


@dataclass
class PauseNode:
    """A workflow pause — a resumable stop, NOT an error, whatever drove it
    (ctrl-c / ctrl-d / interrupt, or an error category like api / max_loops /
    retry_ceiling). Renders as a quiet dim `⏸ paused (reason)` line, mirroring the
    live `_emit_paused` / `_emit_paused_summary` chrome, so `imp log` and the live
    view agree. For an error-driven pause the underlying failure already appears as
    its own `error:` event just above this line."""
    reason: str
    collapsible = False


@dataclass
class RenameNode:
    name: str
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
class LiteralTextNode:
    """A plain string output field (e.g. coder.summary, reviewer notes) surfaced
    as bold agent-style prose. Distinct from AgentTextNode, which holds a provider
    payload and lazily parses it — this node carries a ready string (issues 5/6)."""
    text: str
    collapsible = False


@dataclass
class ReviewTally:
    approve: int
    total: int
    blocking: int
    nits: int


@dataclass
class PanelSummaryNode:
    """Collapsed one-line summary of a clean/unanimous review panel."""
    tally: ReviewTally
    collapsible = False


@dataclass
class BreadcrumbLine:
    """The merged per-step completion line: `✓ <step> · <N>s` with short scalar
    output fields and a `· → <action>` suffix (omitted for a plain `continue`).
    Replaces the rendered OutcomeLine at step completion (issue 3)."""
    step_name: str
    kind: str | None = None
    elapsed_s: float | None = None
    action: Any = None                 # dict {verb, target?} from step_completed
    route: str | None = None           # resolved routing suffix, same semantics as
                                       # StepNode.quiet_line: None = unresolved,
                                       # "" = resolved-but-silent (same-phase skip),
                                       # "→ …" = a rendered arrow. Read by the loud
                                       # render/live paths (the goto breadcrumb).
    extras: list[str] = field(default_factory=list)
    show_complete: bool = True         # always True (§4): chrome steps now print
                                       # a start header, so the breadcrumb always
                                       # carries the word "complete".
    interrupted: bool = False
    collapsible = False


@dataclass
class FallbackLine:
    event_type: str
    payload: dict[str, Any]
    collapsible = False


@dataclass
class MergedNode:
    """Terminal change_merged event — rendered as the friendly footer in imp log only (F5)."""
    change_id: str
    base: str
    collapsible = False


@dataclass
class TiersResolvedNode:
    """Run-level summary of once-per-run tier resolution (the tiers_resolved
    event). Each row: {tier, provider, model, differ_tier, differ_from}.
    differ_tier/differ_from are None for unconstrained tiers."""
    rows: list[dict[str, Any]] = field(default_factory=list)
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
        # step_completed pops it. Every kind (agent/script/branch) is a
        # normal step — there is no nested gate container.
        self._step_stack: list[StepNode] = []
        self._current_burst: ToolBurstFold | None = None
        # subagent routing: "sub:<event_id>" -> fold (for delegating sub events)
        self._sub_by_agent_id: dict[str, SubagentFold] = {}
        # Two-dict open-fold registry. By block id (NON-EMPTY block ids only)
        # and by tool_call event id (always present, unique per mint). The close
        # path probes the namespace named by the result's `id_ns`; codex keys by
        # event id (empty block), in-process by block id.
        self._open_sub_by_block: dict[str, SubagentFold] = {}
        self._open_sub_by_event: dict[str, SubagentFold] = {}
        self._subagent_failures: dict[tuple[str | None, str], int] = {}
        # True for the window between a workflow_resumed event and the first
        # re-emitted step_started, so resume re-entry can reuse nodes instead of
        # duplicating them (see Task 10B). Default False; goto loops never set it.
        self._resuming = False
        # A step whose forward goto's target phase is not known until the
        # target's step_started arrives (the arrow names the target PHASE). Holds
        # (step_node, breadcrumb, target_step_name, gate_phase) between the two
        # events; gate_phase lets a SAME-phase forward goto resolve to silence.
        # Serves loud steps (breadcrumb.route) and quiet steps (quiet_line) alike.
        self._pending_quiet: tuple[StepNode, BreadcrumbLine, str, str | None] | None = None
        # True between an awaiting_user_input event and the user_message that
        # answers it, so the next user_message is tagged as a genuine human turn
        # (rendered as `❯ …`) rather than a synthetic engine prompt (dropped).
        self._awaiting_reply = False
        # The prompt carried by the awaiting_user_input event that armed
        # _awaiting_reply, handed to the answering user_message as its rendered
        # question line (M3). Reset to "" when consumed.
        self._pending_question = ""
        # Unresolved wait_for_user calls: id → that call's input.prompt. Drives
        # the §1 orphan-result net and call-keyed question attribution (I4).
        self._pending_wfu: dict[str, str] = {}

    # -- public API -----------------------------------------------------------
    @property
    def tree(self) -> NarrativeTree:
        return NarrativeTree(root=self._root, tokens=self._tokens)

    @property
    def current_burst(self) -> ToolBurstFold | None:
        """The open (still-accumulating) tool burst, or None. Public read-only
        accessor for the live driver's finalization check and LiveStatus."""
        return self._current_burst

    def current_activity(self) -> Activity | None:
        """Current activity for the live status line; None when idle."""
        running = [f for f in self._open_sub_by_event.values() if f.running]
        if running:
            label = running[0].current_activity()
            extra = len(running) - 1
            if extra:
                noun = "subagent" if extra == 1 else "subagents"
                label = f"{label} (+{extra} more {noun})"
            return Activity(label=label)
        burst = self._current_burst
        if burst is not None:
            basis = visible_calls(burst)         # keeps killed (recap parity)
            if basis:
                n = len(basis)
                count = f"{n} call{'s' if n != 1 else ''}"
                named = next((c for c in reversed(basis) if not c.killed), None)
                detail = None
                if named is not None:
                    arg = _sanitize_activity_arg(
                        next(iter((named.input or {}).values()), ""))
                    detail = f"{display_tool_name(named.name)} {arg}".rstrip()
                return Activity(count=count, detail=detail)
            # empty filtered basis → fall through to the per-kind rule
        if self._step_stack:
            step = self._step_stack[-1]
            return Activity(label="running" if step.kind in CHROME_KINDS else "thinking")
        if self._current_phase is not None:
            return Activity(label=f"{self._current_phase.name} · working")
        return None

    def _resume_close_folds(self, agent_id: str, event: Any) -> None:
        """Close every still-open fold opened by `agent_id` (the resuming parent).
        Outcome-free close — there is no task tool_result to read — so the fold
        renders as `running=False` with no glyph. Matches opener_agent_id EXACTLY;
        skips folds opened by a different parent (single-level routing means there
        is at most one today, but the loop is robust to future nesting)."""
        ts = event.get("timestamp") if isinstance(event, dict) else None
        to_close = [
            fold for fold in self._open_sub_by_event.values()
            if fold.running and fold.opener_agent_id == agent_id
        ]
        for fold in to_close:
            self._open_sub_by_block.pop(fold.block_id, None)
            self._open_sub_by_event.pop(fold.event_id, None)
            _close_subagent_fold(fold, {}, ts)

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

        # 1b. resume-close fallback. When the PARENT that opened a fold emits its
        #     next event — control has returned from the synchronous
        #     `await dispatch_task` — close any still-open fold for that parent.
        #     This salvages a fold whose `task` tool_result is missing/lost (the
        #     canonical block-id close in _h_tool_result never fired).
        #
        #     H1 GUARD (essential): the task tool_result is ITSELF a parent-agent_id
        #     event. SKIP resume-close when the current event is a `tool_result`
        #     whose payload id MATCHES an open fold (block- OR event-keyed; both
        #     dicts are probed below) — let _h_tool_result do the outcome-carrying
        #     close. Popping here first would leave _h_tool_result nothing to close
        #     (no outcome + an orphan "tool" line, regressing every provider). Keep
        #     the guard PRECISE: skip only for a MATCHING tool_result, never a
        #     non-matching orphan (which must still trigger resume-close, else an
        #     old fold re-freezes).
        #
        #     IGNORE agent_id-None events (tiers_resolved, workflow_*): they don't
        #     belong to any opener and must not close a sibling's fold. A `task`
        #     tool_call is excluded below (is_task_call): concurrent batched siblings
        #     must NOT resume-close each other — a back-to-back/parallel `task`
        #     dispatch keeps prior folds open until their own results close them.
        if isinstance(agent_id, str) and (self._open_sub_by_block or self._open_sub_by_event):
            rid_ = str(payload.get("id", ""))
            # A `task` tool_call is a concurrent sibling dispatch in a batch, not
            # a signal that control returned — it must NOT resume-close siblings.
            is_task_call = etype == "tool_call" and payload.get("name") == "task"
            matched = (
                etype == "tool_result"
                and (rid_ in self._open_sub_by_block or rid_ in self._open_sub_by_event))
            if not matched and not is_task_call:
                self._resume_close_folds(agent_id, event)

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
    self._root.slug = p.get("slug")
    self._root.title = p.get("title", "")
    self._root.change_id = p.get("id")


NarrativeBuilder._HANDLERS["change_created"] = _h_change_created


_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}-")


def _slug_descriptor(slug: str) -> str:
    return _DATE_PREFIX.sub("", slug)


def _h_change_renamed(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # A rename carries the new title + slug as a unit (current schema). Apply them
    # together; ignore a legacy/partial event missing either, rather than leaving
    # the header half-updated (e.g. new slug beside a stale title).
    new_title = p.get("new_title")
    new_slug = p.get("new_slug")
    if new_title is None or new_slug is None:
        return
    self._root.title = new_title
    self._root.slug = new_slug
    self._append(RenameNode(name=_slug_descriptor(new_slug)))


NarrativeBuilder._HANDLERS["change_renamed"] = _h_change_renamed


def _h_change_merged(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_burst()
    self._root.status = "completed"
    descriptor = self._root.change_id or _slug_descriptor(self._root.slug or "")
    # Append at the WORKFLOW ROOT (not the still-open phase): the merged footer is a
    # run-level terminal line that ends the whole transcript, after every phase.
    self._root.children.append(MergedNode(change_id=descriptor, base=str(p.get("base", ""))))


NarrativeBuilder._HANDLERS["change_merged"] = _h_change_merged


def _h_merge_blocked(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # _emit_merge_blocked already renders the friendly ⚠ block; suppress the raw fallthrough.
    self._flush_burst()


NarrativeBuilder._HANDLERS["merge_blocked"] = _h_merge_blocked


def _ensure_phase(self: NarrativeBuilder, name: str) -> None:
    """Derive the phase grouping from step_started.payload.phase. When the phase
    name changes from the current open one, lazily create a PhaseNode, append it
    to the workflow root, and make it the current scope. There are no
    phase_started/phase_completed events — phase is purely DERIVED here."""
    if (self._current_phase is not None and not self._current_phase.closed
            and self._current_phase.name == name):
        return
    if self._current_phase is not None:
        self._current_phase.closed = True   # let the live cursor pop the drained phase
    phase = PhaseNode(name=name)
    self._root.children.append(phase)
    self._current_phase = phase
    self._step_stack.clear()


def _apply_route(node: StepNode, breadcrumb: BreadcrumbLine, suffix: str) -> None:
    if node.quiet:
        node.quiet_line = suffix
    breadcrumb.route = suffix


def _h_step_started(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_burst()
    name = p.get("step", "?")
    kind = p.get("kind")
    phase = p.get("phase", "?")
    if self._pending_quiet is not None:
        qnode, breadcrumb, target, gate_phase = self._pending_quiet
        if phase == gate_phase:
            # SAME-phase forward goto (e.g. panel-gate → recheck, both in review):
            # an internal skip, not a phase transition — render nothing. "" is the
            # resolved-but-silent sentinel, distinct from None (= unresolved, which
            # holds the live cursor in live_driver._is_final).
            _apply_route(qnode, breadcrumb, "")
        else:
            # Cross-phase: the forward goto runs its target next, so this step IS
            # the target and its phase is the arrow's phase. Fall back to the raw
            # target name if a different step somehow intervenes (defensive; never
            # the shipped path).
            _apply_route(qnode, breadcrumb, f"→ {phase if name == target else target}")
        self._pending_quiet = None
    # Retry re-entry: the executor's transient-error retry loop re-emits
    # step_started on every iteration WITHOUT a prior step_completed for that
    # step, so the same StepNode is still on the stack. Reuse it in place —
    # do NOT clear children (unlike resume re-entry below), because every
    # attempt's error/warning lines must stay visible as direct children of
    # the one step. Do not reset start_ts/model/provider/kind/interactive:
    # elapsed time should reflect the operator's wait, and the other fields
    # are identical by construction (same agent, same step). The `not
    # self._resuming` guard makes this disjoint from resume re-entry below
    # (spec: "the two paths are disjoint"): on a resume the step from before
    # the pause is also on the stack with matching (name, phase), so without
    # this guard the retry branch would fire first and swallow the resume —
    # never clearing the stale partial transcript the resume logic exists to
    # clear.
    if (not self._resuming
            and self._step_stack
            and self._current_phase is not None
            and not self._current_phase.closed
            and self._current_phase.name == phase):
        top = self._step_stack[-1]
        if not top.closed and top.name == name:
            return
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
            existing.interrupted = False
            existing.kind = kind
            existing.model = p.get("model")
            existing.provider = p.get("provider")
            # Interactivity is re-sourced from the event stream (an
            # awaiting_user_input event sets it), NOT from step_started — so
            # reset it to False here; a re-emitted awaiting_user_input on the
            # resumed step re-arms it (Task 8 / L3).
            existing.interactive = False
            existing.start_ts = event.get("timestamp")
            existing.agent_output = None
            self._step_stack = [existing]
            return
    _ensure_phase(self, phase)
    # interactive defaults False — it is re-sourced from the event stream (an
    # awaiting_user_input event marks the step interactive); step_started no
    # longer carries an interactive key.
    node = StepNode(name=name, kind=kind, model=p.get("model"),
                    provider=p.get("provider"),
                    start_ts=event.get("timestamp"),
                    quiet=bool(p.get("quiet", False)))
    self._append(node)
    self._step_stack.append(node)


def _h_step_completed(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_burst()
    action = p.get("action")          # dict {verb, target?}; var_mutations is a sibling
    node = self._pop_step(p.get("step"))
    if node is not None:
        # Un-hold any trailing held narration — agent_output won't fire for
        # steps that end without a complete_step call.
        narration_idx = _last_narration_index(node.children)
        if narration_idx is not None:
            node.children[narration_idx].held = False
        # Preserve StepNode.outcome (existing tests assert on the field); the
        # RENDERED outcome is subsumed by the merged breadcrumb (issue 3).
        node.outcome = OutcomeLine(action=action)
        node.end_ts = event.get("timestamp")
        elapsed = _elapsed_seconds(node.start_ts, event.get("timestamp"))
        breadcrumb = BreadcrumbLine(
            step_name=node.name, kind=node.kind, elapsed_s=elapsed, action=action,
            extras=_breadcrumb_extras(node.agent_output),
            interrupted=node.interrupted)
        node.children.append(breadcrumb)
        node.closed = True
        if isinstance(action, dict) and action.get("verb") == "goto":
            if action.get("resume") or action.get("rework"):
                # resume edge OR a backward goto carrying feedback: a rejection.
                _apply_route(node, breadcrumb, "→ reworking")
            else:
                # forward goto: the arrow names the TARGET phase, known only once
                # the target's step_started arrives (resolved there). Capture the
                # gate's own phase so a same-phase skip resolves silent.
                gate_phase = self._current_phase.name if self._current_phase else None
                self._pending_quiet = (node, breadcrumb,
                                       str(action.get("target", "")), gate_phase)


NarrativeBuilder._HANDLERS["step_started"] = _h_step_started
NarrativeBuilder._HANDLERS["step_completed"] = _h_step_completed


def _h_workflow_paused(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # EVERY pause is a pause, not an error — a resumable stop. Live frames them all
    # the same way (_emit_paused / _emit_paused_summary print a dim `⏸ … paused`),
    # so `imp log` renders a matching PauseNode rather than an `error:` line. The
    # reason that drove it (ctrl-c, api, max_loops, retry_ceiling, …) rides the ⏸
    # line; for an error-driven pause the underlying failure was already shown as
    # its own `error:` event just above, so re-stating it as an error here would
    # both double up and mismatch the live view (the bug this fixes).
    self._flush_burst()
    self._root.status = "paused"
    reason = p.get("interrupt_reason") or p.get("reason")
    if reason:
        self._append(PauseNode(reason=reason))


def _h_workflow_resumed(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_burst()
    self._root.status = "running"
    # Finalize the prior attempt's dangling folds (the unsettled subset; settled
    # siblings already closed via their logged tool_results) as interrupted so
    # they stop accumulating events and stop shadowing current_activity().
    for fold in self._open_sub_by_event.values():
        fold.running = False
        fold.terminal_state = "interrupted"
        # Drop the interrupted fold's membership entry too — otherwise a late
        # `sub:` event for it would still route in (exact-membership above) and
        # keep accumulating onto an interrupted fold, the very thing this
        # finalize exists to stop. Settled pre-pause folds keep their entries.
        self._sub_by_agent_id.pop(f"sub:{fold.event_id}", None)
    self._open_sub_by_block.clear()
    self._open_sub_by_event.clear()
    self._resuming = True                # open the re-entry window


NarrativeBuilder._HANDLERS["workflow_paused"] = _h_workflow_paused
NarrativeBuilder._HANDLERS["workflow_resumed"] = _h_workflow_resumed


def _h_awaiting_user_input(
    self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any],
) -> None:
    # The authoritative signal that this step is soliciting the human (Task 8):
    # mark the CURRENT open step interactive (drives the conversational-beat
    # blank-line spacing in render_text), and arm the flag so the next
    # user_message is tagged as a genuine human turn (rendered as `> …`) rather
    # than a synthetic engine prompt (dropped). The event's prompt payload is
    # carried onto the pending reply as its question line (M3); render nothing
    # here — the reply node renders the question above its caret.
    if self._step_stack:
        self._step_stack[-1].interactive = True
    self._awaiting_reply = True
    self._pending_question = str(p.get("prompt") or "")
    return None


def _h_user_interjected(
    self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any],
) -> None:
    # A human interject is delivered UNPROMPTED (no preceding awaiting_user_input),
    # so deliver_interject emits user_message THEN user_interjected (that emit order
    # is a deliberate prior decision — keep it). The user_message just appended a
    # UserMessageNode with interactive=False, which render_text would drop as a
    # synthetic engine prompt. Retroactively mark that node interactive=True so it
    # renders as a genuine `> …` human turn. It carries no question (unsolicited),
    # so pending_question stays "". The node is the most-recently-appended child of
    # the current container (post-append mutation is an established pattern — see
    # the resume re-entry path mutating existing.interactive).
    children = self._container().children
    if children and isinstance(children[-1], UserMessageNode):
        children[-1].interactive = True
    return None


NarrativeBuilder._HANDLERS["awaiting_user_input"] = _h_awaiting_user_input
NarrativeBuilder._HANDLERS["user_interjected"] = _h_user_interjected


def _sanitize_activity_arg(value: Any, *, limit: int = 50) -> str:
    """Reduce a tool's first arg to a single short status-line token. For `bash`
    the first arg is `command` — often a whole multi-line script — and the spinner
    redraws it every 0.1s, flooding the terminal (issue 4). Collapse all whitespace
    (including newlines) into single spaces, then truncate with an ellipsis."""
    text = str(value)
    collapsed = " ".join(text.split())
    if len(collapsed) > limit:
        return collapsed[:limit] + "…"
    return collapsed


def _user_message_text(payload: dict[str, Any]) -> str:
    """Inline (pure) extraction of user chat text. Deliberately NOT
    render_helpers.user_message_text — importing that module pulls the provider
    parse chain at module load (D3). This logic is trivial; keep it local."""
    return "".join(
        b.get("text", "") for b in (payload.get("content") or [])
        if isinstance(b, dict) and b.get("type") == "text")


def _sole_success_tool_result(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The payload's content when it is exactly one success-shaped tool_result
    block, else None. The resume-reply rule keys on this shape PLUS the armed
    flag (the disjointness argument)."""
    content = payload.get("content") or []
    if len(content) != 1:
        return None
    b = content[0]
    if not isinstance(b, dict) or b.get("type") != "tool_result" or b.get("is_error"):
        return None
    return b


def _is_prose_field(key: str, value: Any) -> bool:
    """True for a free-text output field that should render as a bold prose block
    rather than fold into the breadcrumb (issues 5/6)."""
    return (isinstance(value, str) and not key.endswith("_path")
            and ("\n" in value or len(value) > _PROSE_THRESHOLD))


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _normalized_tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def _near_duplicate(a: str, b: str, *, threshold: float = 0.8) -> bool:
    ta, tb = _normalized_tokens(a), _normalized_tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= threshold


def _last_narration_index(children: list[Any]) -> int | None:
    """Trailing AgentTextNode index, scanning past any trailing ToolBurstFold nodes."""
    i = len(children) - 1
    while i >= 0 and isinstance(children[i], ToolBurstFold):
        i -= 1
    if i >= 0 and isinstance(children[i], AgentTextNode):
        return i
    return None


def _breadcrumb_extras(args: Any) -> list[str]:
    """Short scalar output fields (`status`, `verdict`, `*_path`, numbers, bools,
    short strings) folded into the breadcrumb as `key=value`. Prose fields and
    non-scalar values (dicts/lists) are skipped."""
    if not isinstance(args, dict):
        return []
    out: list[str] = []
    for k, v in args.items():
        if _is_prose_field(k, v):
            continue
        if isinstance(v, (str, int, float, bool)):
            out.append(f"{k}={v}")
    return out


def _parse_member_outcome(fold: SubagentFold) -> tuple[str, str]:
    if fold.is_error:
        return "task_gap", fold.outcome or ""
    try:
        data = json.loads(fold.outcome or "")
    except (ValueError, TypeError):
        return "task_gap", fold.outcome or ""
    if not isinstance(data, dict):
        return "task_gap", str(data)
    return str(data.get("verdict") or ""), str(data.get("findings") or "")


def panel_member_tally(step: StepNode) -> ReviewTally:
    """Count a review-panel step's member verdicts."""
    approve = total = blocking = nits = 0
    for fold in step.children:
        if not isinstance(fold, SubagentFold):
            continue
        total += 1
        verdict, findings = _parse_member_outcome(fold)
        if verdict == "approve":
            approve += 1
            if findings.strip():
                nits += 1
        else:
            blocking += 1
    return ReviewTally(approve=approve, total=total, blocking=blocking, nits=nits)


def _find_last_step(root: WorkflowNode, name: str) -> StepNode | None:
    found: StepNode | None = None

    def walk(nodes: list[Any]) -> None:
        nonlocal found
        for n in nodes:
            if isinstance(n, StepNode):
                if n.name == name:
                    found = n
                walk(n.children)
            elif isinstance(n, PhaseNode):
                walk(n.children)
    walk(root.children)
    return found


def _single_reviewer_tally(output: dict[str, Any]) -> ReviewTally:
    verdict = str(output.get("verdict") or "")
    findings = str(output.get("findings") or "")
    if verdict == "approve":
        return ReviewTally(approve=1, total=1, blocking=0,
                           nits=1 if findings.strip() else 0)
    return ReviewTally(approve=0, total=1, blocking=1, nits=0)


def review_tally(tree: NarrativeTree) -> ReviewTally | None:
    """Merge-gate verdict counts: panel member verdicts when the `review` step ran
    its opus panel, else the single generalist reviewer on fast-path/re-entry."""
    review = _find_last_step(tree.root, "review")
    if review is None:
        return None
    if any(isinstance(c, SubagentFold) for c in review.children):
        return panel_member_tally(review)
    if isinstance(review.agent_output, dict):
        return _single_reviewer_tally(review.agent_output)
    return None


def phase_elapsed(phase: PhaseNode) -> float | None:
    """Phase duration derived from its steps: first step start_ts → last step
    end_ts. No phase_started/phase_completed events exist (§7) — phase timing is
    derived. None when the phase has no timestamped steps."""
    steps = [c for c in phase.children if isinstance(c, StepNode)]
    if not steps:
        return None
    return _elapsed_seconds(steps[0].start_ts, steps[-1].end_ts)


def _elapsed_seconds(start_ts: str | None, end_ts: Any) -> float | None:
    """Diff two ISO-8601 timestamps (state.py writes datetime.now(UTC).isoformat()).
    Returns None when either endpoint is missing or unparseable (issue 3)."""
    if not start_ts or not isinstance(end_ts, str) or not end_ts:
        return None
    try:
        return (datetime.fromisoformat(end_ts)
                - datetime.fromisoformat(start_ts)).total_seconds()
    except (ValueError, TypeError):
        return None


def _last_prose_text(self: NarrativeBuilder) -> str:
    """Text of the most recent agent-prose child in the open container, or "".
    Used to suppress a pending_question that merely duplicates the agent's
    end-of-turn message (the usual attended case: the solicited prompt IS the
    assistant text that just rendered)."""
    children = self._container().children
    for child in reversed(children):
        if isinstance(child, AgentTextNode):
            return child.text.strip()
        if isinstance(child, LiteralTextNode):
            return child.text.strip()
        if isinstance(child, (ThinkingFold, ToolBurstFold, SkillLoadNode)):
            continue  # interstitial chrome; keep scanning for the prose turn
        break  # any other node type means no prose is adjacent — show the question (never wrong)
    return ""


def _h_user_message(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_burst()
    interactive, self._awaiting_reply = self._awaiting_reply, False
    question, self._pending_question = self._pending_question, ""
    text = _user_message_text(p)
    if interactive and not text.strip():
        block = _sole_success_tool_result(p)
        if block is not None:
            # Live-safe resume reply: the injected message's sole block is a
            # success tool_result carrying the human's reply (anthropic/openai/
            # claude_code resume shape). A known dangling id is a strengthener.
            text = _summarize_result(block.get("content"))
            rid = str(block.get("tool_use_id") or block.get("id") or "")
            self._pending_wfu.pop(rid, None)
    # M3: the event's prompt is the only GUARANTEED source of the question (the
    # attended tool path solicits with ""). But in the usual case the agent's
    # end-of-turn assistant text — already rendered as prose just above — IS the
    # prompt, so echoing it as a pending-question line would duplicate the whole
    # turn. Carry the question ONLY when it isn't already that preceding prose.
    # A synthetic prompt (no preceding awaiting_user_input) carries no question.
    if interactive and question and question.strip() != _last_prose_text(self):
        rendered_question = question
    else:
        rendered_question = ""
    self._append(UserMessageNode(
        text=text, interactive=interactive, pending_question=rendered_question))


def _h_llm_response(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # Token accumulation already happened in add(); here we only build the node.
    # §8: an empty-text llm_response is a tool-use-turn carrier (a lone tool_use
    # block). It must NOT flush an open burst — otherwise consecutive tool calls
    # each start a fresh burst and the status counter sticks at 1. Compute text
    # FIRST, then flush+append only on a genuine prose turn (this reorders the
    # original flush-before-compute).
    node = AgentTextNode(payload=p)
    if node.text.strip():
        self._flush_burst()
        # Un-hold any trailing held narration — a new one is taking its place.
        children = self._container().children
        prev_idx = _last_narration_index(children)
        if prev_idx is not None and children[prev_idx].held:
            children[prev_idx].held = False
        node.held = True
        self._append(node)


def _h_thinking(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # §8: a thinking block that is part of a tool-use turn must not flush an open
    # burst (consecutive tools accumulate). A standalone thinking turn is harmless
    # to leave un-flushed: the next real prose / step boundary flushes the burst.
    self._append(ThinkingFold(content=str(p.get("content", ""))))


def _h_step_context(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # step_context is the agent's first event; do NOT flush — it opens, not
    # interrupts, the content stream. (No burst can be open before it anyway.)
    self._append(StepContextNode(payload=p))


def _h_agent_output(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # agent_output (= complete_step args) fires BEFORE step_completed. Stash the
    # args on the open StepNode so _h_step_completed can fold short scalars into
    # the breadcrumb, and surface each long/multi-line free-text field as a bold
    # prose block now (issues 5/6). Enum/scalar/*_path fields are not prose.
    if not self._step_stack:
        return
    self._flush_burst()
    step = self._step_stack[-1]
    step.agent_output = p
    if (step.name == "review" and p.get("verdict") == "approve"
            and any(isinstance(c, SubagentFold) for c in step.children)):
        # member detail survives in imp log --full via the expanded subagent folds
        narration_idx = _last_narration_index(self._container().children)
        if narration_idx is not None:
            self._container().children[narration_idx].held = False
        self._append(PanelSummaryNode(tally=panel_member_tally(step)))
        return
    children = self._container().children
    narration_idx = _last_narration_index(children)
    for k, v in p.items():
        # `_is_prose_field` already screens most empties; the `.strip()` is
        # belt-and-suspenders for a whitespace-only multi-line field.
        if _is_prose_field(k, v) and v.strip():
            if (narration_idx is not None
                    and _near_duplicate(children[narration_idx].text, v)):
                children.pop(narration_idx)
                narration_idx = None
            self._append(LiteralTextNode(text=v))
    # Un-hold the narration if it wasn't popped: it's now final for the live driver.
    if narration_idx is not None:
        children[narration_idx].held = False


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
    if burst.start_ts is None:
        burst.start_ts = event.get("timestamp")  # burst clock starts at first call
    call = ToolCall(
        call_id=str(p.get("id", "")), name=str(p.get("name", "tool")),
        input=p.get("input") or {}, call_ts=event.get("timestamp"))
    burst.calls.append(call)
    if call.name == "wait_for_user":
        self._pending_wfu[call.call_id] = str((call.input or {}).get("prompt", ""))


def _close_subagent_fold(fold: SubagentFold, p: dict[str, Any],
                         end_ts: str | None = None) -> None:
    fold.running = False
    fold.end_ts = end_ts
    fold.is_error = bool(p.get("error", False))
    fold.outcome = _summarize_result(p.get("content"))
    # Terminal state for Part D's glyph. Prefer an explicit payload kind (forward
    # compatible); else detect the bracketed marker dispatch_task embeds in the
    # failure message ("... failed (config) [stalled]"). The brackets keep the
    # match precise (low false-positive). 'interrupted' is matched too for
    # forward compatibility, though no subagent path emits it today.
    kind = p.get("kind")
    if kind not in ("stalled", "interrupted"):
        # The bracketed marker only originates from the failure path; a
        # SUCCESSFUL subagent's content is agent-controlled JSON that may
        # legitimately contain "[stalled]". Gate the scan on is_error so such
        # text can't false-positive into a terminal state.
        text = fold.outcome if fold.is_error else ""
        kind = next(
            (s for s in ("stalled", "interrupted") if f"[{s}]" in (text or "")),
            None)
    if kind in ("stalled", "interrupted"):
        fold.terminal_state = kind


def _take_subagent_fold(
    self: NarrativeBuilder, rid: str, id_ns: Any,
) -> tuple[SubagentFold | None, str]:
    """Probe the id_ns namespace for rid; remove from both dicts. Returns (fold, status)."""
    if id_ns == "event":
        fold = self._open_sub_by_event.get(rid)
    elif id_ns == "block":
        fold = self._open_sub_by_block.get(rid)
    elif id_ns is None:
        fold = self._open_sub_by_block.get(rid) or self._open_sub_by_event.get(rid)
    else:
        fold = None
    if fold is not None:
        self._open_sub_by_block.pop(fold.block_id, None)
        self._open_sub_by_event.pop(fold.event_id, None)
        return fold, "matched"
    return (None, "unstamped") if id_ns is None else (None, "stamped_miss")


def _h_tool_result(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    rid = str(p.get("id", ""))
    ts = event.get("timestamp")
    is_error = bool(p.get("error", False))
    footer = _format_citations_footer(p.get("citations"))
    summary = _summarize_result(p.get("content"))

    # Closing a subagent? Probe the id_ns-named namespace (two-dict scheme). A
    # stamped result that misses its namespace is a malformed-log warning, never
    # an arbitrary fold close; unstamped legacy results take the ordered probe.
    fold, status = _take_subagent_fold(self, rid, p.get("id_ns"))
    if fold is not None:
        _close_subagent_fold(fold, p, ts)
        if fold.is_error or fold.terminal_state in ("stalled", "interrupted"):
            key = (fold.opener_agent_id, fold.subagent_name)
            self._subagent_failures[key] = self._subagent_failures.get(key, 0) + 1
        return
    if status == "stamped_miss":
        self._append(ErrorNode(
            message=f"task result {rid!r} (id_ns={p.get('id_ns')!r}) matched no open fold",
            is_warning=True))
        return

    pending_prompt = self._pending_wfu.get(rid)

    # Normal burst result: pair with the matching open call.
    burst = self._current_burst
    if burst is not None:
        for call in burst.calls:
            if call.call_id == rid and call.result is None:
                if call.name == "wait_for_user":
                    self._pending_wfu.pop(rid, None)
                    if is_error:
                        # Failed dispatch: keep the call (renders as failed per §2),
                        # no split, no turn; the solicit is over so consume flags.
                        call.result = summary
                        call.citations_footer = footer
                        call.is_error = True
                        self._awaiting_reply = False
                        self._pending_question = ""
                        return
                    _synthesize_wfu_turn(
                        self, text=summary,
                        question=str((call.input or {}).get("prompt", "")),
                        answered_call=call)
                    return
                # Non-wfu pairing: this turn actually ran → advance the clock.
                burst.end_ts = ts
                call.result = summary
                call.citations_footer = footer
                call.is_error = is_error
                return

    # Pending-id net — BEFORE the orphan fallthrough (load-bearing order).
    if pending_prompt is not None:
        self._pending_wfu.pop(rid, None)
        if is_error:
            # Error-shaped result matching a pending id that missed pairing →
            # route to the true-orphan branch, no turn.
            b = _ensure_burst(self)
            b.end_ts = ts
            b.calls.append(ToolCall(
                call_id=rid, name="tool", input={}, result=summary,
                citations_footer=footer, is_error=True, call_ts=ts))
            self._awaiting_reply = False
            self._pending_question = ""
            return
        _synthesize_wfu_turn(self, text=summary, question=pending_prompt,
                             answered_call=None)
        return

    # (The former §11 unmatched-result heuristic — popitem() the most-recent open
    # fold — was removed: the resume-close fallback in add() now closes a fold whose
    # task tool_result is missing/lost, deterministically and outcome-free, when the
    # opener resumes. Part 1 also eliminated the empty-block-id case that §11
    # existed to salvage, so an unmatched result here is a genuine orphan.)
    # Orphan result (malformed log): start/extend a burst with a "tool" entry.
    burst = _ensure_burst(self)
    burst.end_ts = ts
    burst.calls.append(ToolCall(
        call_id=rid, name="tool", input={}, result=summary,
        citations_footer=footer, is_error=is_error, call_ts=ts))


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
        call_id=rid, name=str(p.get("name", "tool")), input={}, killed=True,
        call_ts=event.get("timestamp")))


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


def _format_citations_footer(citations: Any) -> str:
    """Render an optional citations array as a compact 'Sources:' footer.
    Only `url` is guaranteed; `title` is shown when present. Returns "" when
    there is nothing to show."""
    if not citations:
        return ""
    lines: list[str] = []
    for c in citations:
        if not isinstance(c, dict):
            continue
        url = c.get("url")
        if not url:
            continue
        title = c.get("title")
        lines.append(f"- {title} — {url}" if title else f"- {url}")
    if not lines:
        return ""
    return "\n\nSources:\n" + "\n".join(lines)


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


def _synthesize_wfu_turn(self: NarrativeBuilder, *, text: str, question: str,
                         answered_call: ToolCall | None) -> None:
    """Split the open burst and append the synthesized user turn (I1/I3). Child
    order: [closed burst, user turn, fresh open burst]. Carries unresulted
    siblings (result is None — including killed-unresulted) into the fresh burst,
    whose start_ts is the earliest carried call_ts. No carried siblings → no
    fresh burst; _current_burst becomes None. Consumes the awaiting flags."""
    burst = self._current_burst
    self._current_burst = None
    carried: list[ToolCall] = []
    if burst is not None:
        if answered_call is not None and answered_call in burst.calls:
            burst.calls.remove(answered_call)
        carried = [c for c in burst.calls if c.result is None]
        burst.calls = [c for c in burst.calls if c.result is not None]
    # Consume the awaiting flags (mirrors _h_user_message) and dedup the question
    # against the immediately-preceding prose turn.
    self._awaiting_reply = False
    self._pending_question = ""
    q = question if (question and question.strip() != _last_prose_text(self)) else ""
    self._append(UserMessageNode(text=text, interactive=True, pending_question=q))
    if carried:
        fresh = ToolBurstFold()
        cts = [c.call_ts for c in carried if c.call_ts]
        fresh.start_ts = min(cts) if cts else None
        fresh.calls = carried
        self._current_burst = fresh
        self._append(fresh)


NarrativeBuilder._HANDLERS["tool_call"] = _h_tool_call
NarrativeBuilder._HANDLERS["tool_result"] = _h_tool_result
NarrativeBuilder._HANDLERS["tool_killed"] = _h_tool_killed


def _h_skill_load(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # The agent pulled in a skill. Render it once, as its own `load skill …`
    # line; the matching `load_skill` tool call is dropped from the burst recap
    # (render_text) so the same action doesn't show up twice. Do NOT flush the
    # open burst: this event fires BETWEEN a tool_call and its tool_result (the
    # load_skill round-trip), so flushing would orphan that pending result into a
    # stray `tool` burst.
    self._append(SkillLoadNode(name=str(p.get("name", "?")), source=p.get("source")))


NarrativeBuilder._HANDLERS["skill_load"] = _h_skill_load


def _h_error(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_burst()
    if p.get("category") == "config" and p.get("kind") == "interrupted":
        if self._step_stack:
            self._step_stack[-1].interrupted = True
        return
    detail = ""
    d = p.get("detail")
    if isinstance(d, dict):
        detail = str(d.get("full_summary") or "")
    self._append(ErrorNode(
        message=str(p.get("message", "?")), category=p.get("category"),
        is_warning=False, detail=detail))


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


def _h_tiers_resolved(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # Fires once at run start with agent_id=None, BEFORE any step_started, so the
    # step stack is empty and _append lands this at the workflow root.
    # Payload: {"tiers": {tier: {provider, model, ...}}, "steps": {step: {...}}}
    tiers = p.get("tiers") or {}
    rows = [
        {"tier": tier, "provider": d.get("provider"), "model": d.get("model"),
         "differ_tier": d.get("differ_tier"), "differ_from": d.get("differ_from"),
         "warning": d.get("warning")}
        for tier, d in tiers.items()
    ]
    # Also add step overrides (steps with differ_from resolution)
    steps = p.get("steps") or {}
    for step_name, d in steps.items():
        rows.append({
            "tier": step_name, "provider": d.get("provider"), "model": d.get("model"),
            "differ_tier": d.get("differ_tier"), "differ_from": d.get("differ_from"),
            "warning": d.get("warning"),
        })
    self._append(TiersResolvedNode(rows=rows))


NarrativeBuilder._HANDLERS["tiers_resolved"] = _h_tiers_resolved


def _open_subagent_fold(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    """A `task` tool_call opens a SubagentFold (never part of a burst — §5.3
    rule 2). Routing key is "sub:" + the tool_call EVENT id; the closing
    `task` tool_result is matched by the tool_use BLOCK id (payload['id']).

    NOTE: routing is SINGLE-LEVEL. A grandchild's `sub:<id2>` events are not
    delegated into this fold's nested builder (the top builder only knows its
    own registered child ids). This is currently safe because only `task-lead`
    declares `task` in its tools; its subagents (implementer, reviewers) declare
    no subagents of their own, so `child_subagent_defs` in `dispatch_task` is
    empty for them, and the dispatch guard rejects any undeclared name. The
    depth ceiling (`MAX_SUBAGENT_DEPTH`) provides a hard backstop. If the engine
    ever enables nesting, routing must become recursive/prefix-aware and a
    nested-subagent fixture added."""
    self._flush_burst()
    event_id = str(event.get("id", ""))
    block_id = str(p.get("id", ""))
    agent_id = event.get("agent_id")
    inp = p.get("input") or {}
    fold = SubagentFold(
        subagent_name=str(inp.get("subagent_name", "?")),
        event_id=event_id, block_id=block_id, nested=NarrativeBuilder(),
        opener_agent_id=agent_id if isinstance(agent_id, str) else None,
        start_ts=event.get("timestamp"))
    fold.retry_attempt = self._subagent_failures.get(
        (fold.opener_agent_id, fold.subagent_name), 0)
    self._append(fold)
    self._sub_by_agent_id[f"sub:{event_id}"] = fold
    # Register in both dicts: always by event id (unique per mint), by block id
    # only when non-empty (empty block ids would collide on "").
    self._open_sub_by_event[event_id] = fold
    if block_id:
        self._open_sub_by_block[block_id] = fold


def structural_repr(node: Any) -> Any:
    """A JSON-able structural snapshot of a node subtree, for parity asserts.

    Captures node type, structural fields, and children recursively. Excludes
    only the lazy RenderedMessage cache (an identity/perf detail). Used by the
    §5.4 incremental==batch test and by future regression tests."""
    t = type(node).__name__
    if isinstance(node, WorkflowNode):
        return {"t": t, "slug": node.slug, "title": node.title,
                "change_id": node.change_id, "status": node.status,
                "children": [structural_repr(c) for c in node.children]}
    if isinstance(node, PhaseNode):
        return {"t": t, "name": node.name, "closed": node.closed,
                "children": [structural_repr(c) for c in node.children]}
    if isinstance(node, StepNode):
        return {"t": t, "name": node.name, "kind": node.kind,
                "interactive": node.interactive, "closed": node.closed,
                "end_ts": node.end_ts,
                "outcome": structural_repr(node.outcome) if node.outcome else None,
                "children": [structural_repr(c) for c in node.children]}
    if isinstance(node, AgentTextNode):
        return {"t": t, "text": node.text}
    if isinstance(node, UserMessageNode):
        return {"t": t, "text": node.text, "interactive": node.interactive,
                "pending_question": node.pending_question}
    if isinstance(node, ThinkingFold):
        return {"t": t, "content": node.content}
    if isinstance(node, ToolBurstFold):
        return {"t": t, "calls": [
            {"name": c.name, "call_id": c.call_id, "result": c.result,
             "is_error": c.is_error, "killed": c.killed,
             "citations_footer": c.citations_footer, "input": c.input}
            for c in node.calls]}
    if isinstance(node, SkillLoadNode):
        return {"t": t, "name": node.name, "source": node.source}
    if isinstance(node, SubagentFold):
        return {"t": t, "subagent_name": node.subagent_name, "event_id": node.event_id,
                "block_id": node.block_id, "running": node.running,
                "outcome": node.outcome, "is_error": node.is_error,
                "event_count": node.event_count,
                "nested": [structural_repr(c) for c in node.tree.root.children]}
    if isinstance(node, ErrorNode):
        return {"t": t, "message": node.message, "category": node.category,
                "is_warning": node.is_warning, "detail": node.detail}
    if isinstance(node, PauseNode):
        return {"t": t, "reason": node.reason}
    if isinstance(node, StepContextNode):
        return {"t": t, "payload": node.payload}
    if isinstance(node, OutcomeLine):
        return {"t": t, "action": node.action}
    if isinstance(node, LiteralTextNode):
        return {"t": t, "text": node.text}
    if isinstance(node, PanelSummaryNode):
        t2 = node.tally
        return {"t": t, "tally": [t2.approve, t2.total, t2.blocking, t2.nits]}
    if isinstance(node, BreadcrumbLine):
        return {"t": t, "step_name": node.step_name, "elapsed_s": node.elapsed_s,
                "action": node.action, "extras": node.extras}
    if isinstance(node, FallbackLine):
        return {"t": t, "event_type": node.event_type, "payload": node.payload}
    if isinstance(node, MergedNode):
        return {"t": t, "change_id": node.change_id, "base": node.base}
    return {"t": t}

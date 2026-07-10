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
from datetime import datetime
from typing import Any, ClassVar

from imp.engine.feedback import _truncate

# Tool names omitted from the burst recap AND the live status basis (§1/§3). They
# render elsewhere (load_skill → its own `loading skill …` line) or are placeholders
# (`tool`).
# update_step_label would register as activity in the very status line it exists to
# keep clean.
_OMIT: frozenset[str] = frozenset({"load_skill", "tool", "update_step_label"})

# Chrome step kinds (§4 status precedence). A frozenset so the constant cannot drift.
CHROME_KINDS: frozenset[str] = frozenset({"script", "branch", "merge_and_exit_worktree"})

_TURN_END_STOP_REASONS: frozenset[str] = frozenset(
    {"end_turn", "stop_sequence", "max_tokens"})

# OpenAI Responses terminal statuses (stored as stop_reason); non-terminal = not turn-end
_OPENAI_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "incomplete", "failed", "cancelled"})


def _llm_response_is_turn_end(payload: dict[str, Any]) -> bool:
    # load-bearing: codex None = turn-end; claude_code None = mid-stream fragment
    provider = payload.get("provider")
    if provider == "openai":
        return payload.get("stop_reason") in _OPENAI_TERMINAL_STATUSES
    if provider == "codex":
        return True
    return payload.get("stop_reason") in _TURN_END_STOP_REASONS


def llm_response_has_prose(payload: dict[str, Any]) -> bool:
    """True when an llm_response carries a visible prose block. A lone tool_use
    carrier has empty extracted text and returns False."""
    return bool(AgentTextNode(payload=payload).text.strip())


# ── token accumulator (design §7.7 / F1: read usage straight from payload) ──
@dataclass
class TokenTotals:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_creation: int = 0
    cost: float = 0.0

    def add_usage(self, usage: dict[str, Any] | None) -> None:
        if not usage:
            return
        self.input += int(usage.get("input_tokens", 0) or 0)
        self.output += int(usage.get("output_tokens", 0) or 0)
        self.cache_read += int(usage.get("cache_read_input_tokens", 0) or 0)
        self.cache_creation += int(usage.get("cache_creation_input_tokens", 0) or 0)


def usage_prompt_tokens(usage: dict[str, Any] | None) -> int | None:
    """The prompt-side occupancy of ONE llm_response turn: input + both cache reads.
    None when the turn carried no usage."""
    if not usage:
        return None
    # Lazy import to preserve module-import purity (design D3): importing catalog
    # pulls imp.providers.__init__, which eagerly loads the provider SDKs.
    from imp.providers.catalog import context_tokens_from_usage
    return context_tokens_from_usage(usage)


# ── nodes ───────────────────────────────────────────────────────────────────
# `collapsible` is a class attribute (not a dataclass field) so isinstance-free
# code can ask `node.collapsible`. Nodes carry no explicit id field.

_PROSE_THRESHOLD = 80  # issues 5/6: string output fields longer than this (or
                       # multi-line) render as a bold prose block; shorter scalars
                       # and *_path fields fold into the breadcrumb.

@dataclass
class WorkflowNode:
    id: str | None = None
    title: str = ""
    status: str = "running"            # running | paused | completed
    children: list[Any] = field(default_factory=list)
    collapsible = False


@dataclass
class PhaseNode:
    name: str
    children: list[Any] = field(default_factory=list)
    closed: bool = False
    label: str | None = None
    show_label: bool = True
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
    run_start_ts: str | None = None    # test-lane run clock; phase_elapsed reads start_ts
    end_ts: str | None = None          # ISO-8601 from step_completed.timestamp (§7)
    agent_output: dict[str, Any] | None = None  # stashed complete_step args (issues 5/6)
    show_label: bool = True            # step frame (header + breadcrumb) shown
    show_output: bool = False          # surface a script's stdout as a • message
    position_label: bool = False       # step label is a position counter, not a verb
    label: str | None = None           # step display label (step_started.payload.label)
    live_label: str | None = None      # agent-pushed live status label; None when unset
    lane_waiting_node: Any = None      # in-place merge-queue transcript node; None when unset
    lane_wait: bool = False            # parked in a lane semaphore wait (test or merge); live-only
    phase_label: str | None = None     # owning phase's rendered label
    command: str | None = None         # script step's shell command shown on the status line
    resumed: bool = False              # engine resume re-entry (step_started.payload.resumed)
    route_label: str | None = None     # route reason (rule label) of the entering rule
    rotate_phrase: bool = False        # opt-in: rotate status phrase per work segment
    rotated_phrase: str | None = None  # per-segment phrase; live status line only, never persisted
    closed: bool = False
    backoff_wait_s: float = 0.0        # this step's own accumulated backoff-wait
                                       # seconds (timer honesty)
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
    span_start_ts: str | None = None   # surrounding-prose span start (mirrors live timer)
    span_end_ts: str | None = None     # surrounding-prose span end (mirrors live timer)
    collapsible = True


def visible_calls(burst: ToolBurstFold) -> list[ToolCall]:
    """Burst calls minus the _OMIT names — the shared basis for the lean recap
    (render_text) and the live status count/named-call (current_activity)."""
    return [c for c in burst.calls if c.name not in _OMIT]


def display_tool_name(name: str) -> str:
    return name.rsplit("__", 1)[-1] if name.startswith("mcp__") else name


@dataclass
class Activity:
    """The structured live-status description, split into distinct values so the
    renderer never re-slices a composed ` · ` string (design): `position` is the
    phase + step position breadcrumb (never a verb); `verb` is the reality-derived
    activity verb head; `detail` is the live tool/burst tail (it rides alongside the
    verb, never replaces it). Plain text — the driver colors the segments."""
    position: str | None = None
    verb: str | None = None
    detail: str | None = None
    model: str | None = None
    countdown_total: float | None = None
    kind: str | None = None            # active step kind, for context-bar gating at compose time
    lane_wait: bool = False            # active step parked in a lane semaphore wait (test or merge)
    # Row-3 position segments, pre-split (compare=False) so the driver never re-slices `position`.
    phase: str | None = field(default=None, compare=False)     # phase head (step-blue)
    plan: str | None = field(default=None, compare=False)      # plan counter (step-blue)
    counter: str | None = field(default=None, compare=False)   # running step's label (step-blue)


def _join(*parts: str | None) -> str | None:
    out = " · ".join(p for p in parts if p)
    return out or None


def _split_phase_label(label: str | None) -> tuple[str | None, str | None]:
    """Split "<name> phase · plan i/t" into phase head and optional plan-counter tail."""
    if not label:
        return None, None
    head, _sep, tail = label.partition(" · ")
    return head, (tail or None)


@dataclass
class ResetWait:
    verb: str             # a wait verb from _RETRY_VERB (any of the five)
    total_seconds: float  # backoff_seconds from the agent_retry (any backoff cat)


@dataclass
class SkillLoadNode:
    """A skill the agent pulled in mid-run (the skill_load event). Rendered as a
    quiet `loading skill {name}` line — distinct from the redundant `load_skill`
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
    awaits_result: bool = False        # dispatch is block-tracked: an authoritative
                                       # id_ns="block" tool_result WILL close it, so
                                       # the opener's next event must NOT resume-close
                                       # it early (which would freeze a near-instant
                                       # end_ts and drop the run duration live).
    outcome: str | None = None         # result summary when task tool_result lands
    is_error: bool = False
    terminal_state: str | None = None  # 'stalled' or 'interrupted' (both providers
                                       # now emit it via kind= or [bracket]); else None
    start_ts: str | None = None        # parent `task` tool_call.timestamp
    end_ts: str | None = None          # parent `task` tool_result.timestamp
    label: str | None = None
    task_description: str | None = None  # parent's task prompt to this subagent;
                                         # shown truncated on the --full fold header
    collapsible = True

    @property
    def tree(self) -> NarrativeTree:
        return self.nested.tree


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
    retry_ceiling). Renders as a quiet dim `paused (reason)` line, mirroring the
    live `_emit_paused_summary` chrome, so `imp log` and the live
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
    payload and lazily parses it — this node carries a ready string (issues 5/6).
    An optional `color_role` (a NODE_COLORS key) routes it through the colored-line
    path instead of the markdown/prose path; None keeps the prose rendering."""
    text: str
    color_role: str | None = None
    verbatim: bool = False             # render line-by-line, preserving indentation
    collapsible = False


@dataclass
class BreadcrumbLine:
    """The merged per-step completion line: `✓ <step> · <N>s` with short scalar
    output fields and a `· → <action>` suffix (omitted for a plain `continue`).
    Replaces the rendered OutcomeLine at step completion (issue 3)."""
    step_name: str
    step_label: str | None = None      # step DISPLAY label for the completion line
    kind: str | None = None
    elapsed_s: float | None = None
    action: Any = None                 # dict {verb, target?} from step_completed
    extras: list[str] = field(default_factory=list)
    show_complete: bool = True         # always True (§4): chrome steps now print
                                       # a start header, so the breadcrumb always
                                       # carries the word "complete".
    label: str | None = None           # workflow-authored route reason, appended
                                       # after the arrow when present
    phase_label: str | None = None     # owning phase's rendered label
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
class WarningsSummaryNode:
    """Run-level tally of accumulated runner_warning messages, rendered as a
    deduped footer immediately above a terminal outcome line. `lines` is the
    distinct messages in first-seen order paired with their occurrence count;
    `total` is the sum of all occurrences (the header count)."""
    lines: list[tuple[str, int]] = field(default_factory=list)
    total: int = 0
    collapsible = False


@dataclass
class CompletedNode:
    """Terminal workflow_completed footer (imp log only; live line owned by main.py)."""
    change_id: str
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

_RESUMES_WAIT = frozenset({
    "user_message", "llm_response", "thinking", "tool_call",
    "agent_output", "step_started", "step_completed",
})


class NarrativeBuilder:
    """Per-agent_id state machine. add(event) routes one event into the tree.

    Routing order in add() (design §5.3):
      0. accumulate token usage for EVERY llm_response (before sub routing, so
         subagent tokens count toward the change total — F2). latest_prompt_tokens
         (the context-bar value) updates for THIS builder's own agent only: the
         top-level builder (own_agent_id None) records its non-sub: turns, a nested
         builder records the sub: turns of the subagent it represents; a turn that
         belongs to a registered child folds into the cumulative total but does not
         move this builder's bar.
      1. if agent_id is a registered child subagent fold → delegate down.
      2. else dispatch by event type at the current open scope.
    """

    def __init__(self, *, own_agent_id: str | None = None) -> None:
        self._root = WorkflowNode()
        self._warnings: list[str] = []
        self._warnings_flushed: bool = False
        self._pause_outcome_shown: bool = False
        self._tokens = TokenTotals()
        self._latest_prompt_tokens: int | None = None
        self._latest_context_pct: float | None = None
        self._latest_context_window: int | None = None
        # Occupancy (tokens, window) of the most recent llm_response from ANY
        # agent — subagents included, unlike the gauge fields above. Feeds the
        # reincarnating line so it can report the context that actually crossed
        # the checkpoint, which is frequently a subagent's turn (see loko19).
        self._last_llm_context: tuple[int, int] | None = None
        self._latest_checkpoint_threshold: float | int | None = None
        # The "sub:<event_id>" this builder represents (a dispatched subagent's own
        # turns); None on the top-level builder. Gates which llm_response readings move
        # THIS builder's context gauge: the top level records its own (non-sub:) turns;
        # a nested builder records the turns of the subagent it stands for.
        self._own_agent_id = own_agent_id
        self._current_phase: PhaseNode | None = None
        # Stack of open StepNode containers. step_started pushes one;
        # step_completed pops it. Every kind (agent/script/branch) is a
        # normal step — there is no nested gate container.
        self._step_stack: list[StepNode] = []
        self._current_burst: ToolBurstFold | None = None
        self._span_start_ts: str | None = None
        self._reset_wait: ResetWait | None = None
        # Per-agent backoff-wait accounting (timer honesty). This builder stands
        # for exactly one agent, so it sees only that agent's agent_retry / resume
        # events. `_accum` is the sum of completed wait spans (seconds); `_start`
        # is the ISO timestamp the current in-progress wait began, or None.
        self._backoff_wait_accum: float = 0.0
        self._backoff_wait_start: str | None = None
        # subagent routing: "sub:<event_id>" -> fold (for delegating sub events)
        self._sub_by_agent_id: dict[str, SubagentFold] = {}
        # Two-dict open-fold registry. By block id (NON-EMPTY block ids only)
        # and by tool_call event id (always present, unique per mint). The close
        # path probes the namespace named by the result's `id_ns`; codex keys by
        # event id (empty block), in-process by block id.
        self._open_sub_by_block: dict[str, SubagentFold] = {}
        self._open_sub_by_event: dict[str, SubagentFold] = {}
        # Folds closed PROVISIONALLY by a salvage path (resume-close fallback or
        # pause/resume finalize) before their canonical task tool_result arrived.
        # Keyed the same two ways as the open registry so a late stamped result can
        # recognise its already-closed fold (benign race) instead of warning
        # "matched no open fold". The late result is then the AUTHORITATIVE close:
        # it upgrades the outcome-free salvage close. Only salvage paths populate
        # these — a normal close never does, so a true duplicate canonical result
        # (which the providers suppress) still surfaces as a malformed-log warning.
        self._closed_sub_by_block: dict[str, SubagentFold] = {}
        self._closed_sub_by_event: dict[str, SubagentFold] = {}
        # True for the window between a workflow_resumed event and the first
        # re-emitted step_started, so resume re-entry can reuse nodes instead of
        # duplicating them (see Task 10B). Default False; goto loops never set it.
        self._resuming = False
        # True between an awaiting_user_input event and the user_message that
        # answers it, so the next user_message is tagged as a genuine human turn
        # (rendered as `> …`) rather than a synthetic engine prompt (dropped).
        self._awaiting_reply = False
        # Armed by a completion_nudge; the next prose llm_response consumes it and
        # drops a bare WAITING protocol reply (pure plumbing). Survives non-prose events.
        self._completion_nudge_pending = False
        self._opening_intent_node: UserMessageNode | None = None
        # Optimistic reply echo: reconciled by the next user_message.
        self._pending_reply_node: UserMessageNode | None = None
        # phrase rotation: work seen this segment, consumed at each prose/user-message boundary.
        self._segment_had_work = False
        # Deferred rename: rendered after its narration, not at the change_renamed position.
        self._pending_rename: RenameNode | None = None

    # -- public API -----------------------------------------------------------
    @property
    def tree(self) -> NarrativeTree:
        return NarrativeTree(root=self._root, tokens=self._tokens)

    @property
    def latest_prompt_tokens(self) -> int | None:
        """Prompt-token occupancy of the most recent llm_response turn (input + cache),
        or None before any usage has arrived. NOT cumulative."""
        return self._latest_prompt_tokens

    @property
    def latest_context_pct(self) -> float | None:
        """SDK-reported context-usage percentage (0-100) of the most recent
        llm_response turn, or None before any has arrived."""
        return self._latest_context_pct

    @property
    def latest_context_window(self) -> int | None:
        """SDK-reported raw context window of the most recent llm_response turn,
        or None before any has arrived."""
        return self._latest_context_window

    @property
    def latest_checkpoint_threshold(self) -> float | int | None:
        """Configured context-checkpoint threshold; None before tiers_resolved arrives."""
        return self._latest_checkpoint_threshold

    @property
    def current_burst(self) -> ToolBurstFold | None:
        """The open (still-accumulating) tool burst, or None. Public read-only
        accessor for the live driver's finalization check and LiveStatus."""
        return self._current_burst

    @property
    def has_running_subagent(self) -> bool:
        """True while at least one dispatched subagent fold is still running.
        The live driver checks this to suppress the prose-turn timer reset while
        background subagents are in flight — an interleaved prose llm_response
        from the PARENT would otherwise zero the visible `(+N subagents) · Ns`
        elapsed even though the subagents never stopped (w7f56t over-reset)."""
        return any(f.running for f in self._open_sub_by_event.values())

    def running_subagents(self) -> list[SubagentFold]:
        """Running dispatched subagent folds, in dispatch order (the footer's indented rows)."""
        return [f for f in self._open_sub_by_event.values() if f.running]

    def backoff_wait_seconds(self, ref_iso: str | None) -> float:
        """Total backoff-wait seconds for this builder's agent: completed spans
        plus any in-progress span resolved against `ref_iso` (a live `now` or a
        fold's `end_ts` when it closes mid-wait). A `None` ref_iso leaves an
        in-progress span unresolved, counting only completed spans."""
        if self._backoff_wait_start is not None:
            return self._backoff_wait_accum + (
                elapsed_seconds(self._backoff_wait_start, ref_iso) or 0.0)
        return self._backoff_wait_accum

    def current_activity(self) -> Activity | None:
        """Current activity for the live status footer; None when idle."""
        top = self._step_stack[-1] if self._step_stack else None
        top_kind = top.kind if top is not None else None
        top_lane_wait = top.lane_wait if top is not None else False
        top_phase, top_plan = _split_phase_label(top.phase_label if top is not None else None)
        top_counter = (top.live_label or top.label) if top is not None else None
        if self._reset_wait is not None:
            if top is None:
                position = None
            elif top.position_label:
                position = _join(top.phase_label, top.live_label or top.label)
            else:
                position = top.phase_label
            return Activity(
                position=position,
                verb=self._reset_wait.verb,
                model=top.model if top else None,
                countdown_total=self._reset_wait.total_seconds,
                kind=top_kind, lane_wait=top_lane_wait,
                phase=top_phase, plan=top_plan, counter=top_counter,
            )
        if top is not None:
            model = top.model
            if top.position_label:
                # The label is a counter that rides the position row (Activity.counter),
                # never the spinner verb. The rotating phrase drives the spinner line
                # instead; the counter label (live_label/label) is kept off static_verb so
                # it can never leak onto the spinner. A rotate_phrase step has
                # rotated_phrase seeded at step start; a position_label step
                # without rotate_phrase leaves it None and falls back to "thinking".
                position = _join(top.phase_label, top.live_label or top.label)
                static_verb: str | None = top.rotated_phrase
            else:
                position = top.phase_label
                static_verb = top.live_label or top.rotated_phrase or top.label
        else:
            model = None
            position = None
            static_verb = None

        running = [f for f in self._open_sub_by_event.values() if f.running]
        if running:
            # Surface the first running subagent (dispatch order) that is itself
            # parked in a backoff wait, so its verb + countdown reach the top-level
            # activity line even when several subagents are in flight.
            for f in running:
                nested = f.nested.current_activity()
                if nested is not None and nested.countdown_total is not None:
                    return Activity(position=position, verb=nested.verb,
                                    detail=nested.detail, model=model,
                                    countdown_total=nested.countdown_total,
                                    kind=top_kind, lane_wait=top_lane_wait,
                                    phase=top_phase, plan=top_plan, counter=top_counter)
            n = len(running)
            if n > 1:
                return Activity(position=position, verb=f"running {n} subagents",
                                model=model,
                                kind=top_kind, lane_wait=top_lane_wait,
                                phase=top_phase, plan=top_plan, counter=top_counter)
            f = running[0]
            verb = f.label or f"subagent({f.subagent_name})"
            nested = f.nested.current_activity()
            detail = nested.detail if nested is not None else None
            return Activity(position=position, verb=verb, detail=detail, model=model,
                            kind=top_kind, lane_wait=top_lane_wait,
                            phase=top_phase, plan=top_plan, counter=top_counter)

        detail = None
        burst = self._current_burst
        if burst is not None:
            basis = visible_calls(burst)
            if basis:
                n = len(basis)
                count = f"{n} call{'s' if n != 1 else ''}"
                inflight = next(
                    (c for c in reversed(basis)
                     if c.result is None and not c.killed), None)
                tool = None
                if inflight is not None:
                    arg = _sanitize_activity_arg(
                        next(iter((inflight.input or {}).values()), ""))
                    tool = f"{display_tool_name(inflight.name)} {arg}".rstrip()
                detail = _join(count, tool)

        if top is not None:
            if top.kind in CHROME_KINDS:
                return Activity(position=position, verb=static_verb or top.command,
                                detail=detail, model=model,
                                kind=top_kind, lane_wait=top_lane_wait,
                                phase=top_phase, plan=top_plan, counter=top_counter)
            return Activity(position=position, verb=static_verb or "thinking",
                            detail=detail, model=model,
                            kind=top_kind, lane_wait=top_lane_wait,
                            phase=top_phase, plan=top_plan, counter=top_counter)
        # No step on this builder. A dispatched subagent's nested builder reaches
        # here — carry the in-flight tool tail so the PARENT can read `nested.detail`
        # and concatenate it onto the subagent's verb.
        if detail is not None:
            return Activity(detail=detail)
        if self._current_phase is not None:
            return Activity(verb="working")
        return None

    def seed_opening_intent(self, text: str) -> None:
        """Optimistic echo of the opening message: seed the committed opening-intent
        node at root so frame one shows it. Used by the non-TUI
        LiveDriver.seed_opening_intent; _h_opening_intent reconciles the
        authoritative event into it in place."""
        node = UserMessageNode(text=text, interactive=True)
        self._opening_intent_node = node
        self._root.children.append(node)

    def seed_pending_reply(self, text: str) -> None:
        """Optimistic echo for a your-turn reply (terminal via
        LiveDriver.seed_pending_reply): append a pending interactive node; the
        next user_message reconciles into it via the single-outstanding
        awaiting-reply invariant."""
        node = UserMessageNode(text=text, interactive=True)
        self._pending_reply_node = node
        self._append(node)

    def _resume_close_folds(self, agent_id: str, event: Any) -> None:
        """Close every still-open fold opened by `agent_id` (the resuming parent).
        Outcome-free close — there is no task tool_result to read — so the fold
        renders as `running=False` with no glyph. Matches opener_agent_id EXACTLY;
        skips folds opened by a different parent (single-level routing means there
        is at most one today, but the loop is robust to future nesting).

        SKIPS block-tracked folds (awaits_result): their authoritative id_ns="block"
        tool_result WILL close them, so a non-blocking dispatch's opener can keep
        emitting its own events while the subagent runs without freezing the fold's
        row at a near-instant end_ts. A genuine resume still finalizes them as
        interrupted via _h_workflow_resumed's own loop, not this fallback."""
        ts = event.get("timestamp") if isinstance(event, dict) else None
        to_close = [
            fold for fold in self._open_sub_by_event.values()
            if fold.running and fold.opener_agent_id == agent_id
            and not fold.awaits_result
        ]
        for fold in to_close:
            self._open_sub_by_block.pop(fold.block_id, None)
            self._open_sub_by_event.pop(fold.event_id, None)
            self._remember_salvage_closed(fold)
            _close_subagent_fold(fold, {}, ts)

    def _remember_salvage_closed(self, fold: SubagentFold) -> None:
        """Record a provisionally-closed fold so its late canonical tool_result is
        recognised (benign race) rather than warned on. Mirror the open registry's
        keying: by block id only when non-empty (codex mints empty block ids and
        keys by event id; "" must not collide)."""
        if fold.block_id:
            self._closed_sub_by_block[fold.block_id] = fold
        self._closed_sub_by_event[fold.event_id] = fold

    def _close_open_folds_at_step_end(self, ts: str | None) -> None:
        for fold in list(self._open_sub_by_event.values()):
            if not fold.running:
                continue
            self._open_sub_by_block.pop(fold.block_id, None)
            self._open_sub_by_event.pop(fold.event_id, None)
            self._remember_salvage_closed(fold)
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

        if etype in _RESUMES_WAIT:
            if self._backoff_wait_start is not None:
                span = elapsed_seconds(self._backoff_wait_start, event.get("timestamp")) or 0.0
                self._backoff_wait_accum += span
                if self._step_stack:
                    self._step_stack[-1].backoff_wait_s += span
                self._backoff_wait_start = None
            self._reset_wait = None

        # 0. token accumulation — before sub routing (F2) so subagent events fold
        #    into the change total. The context-bar value (_latest_prompt_tokens)
        #    tracks THIS builder's own agent: the top level (own_agent_id None) records
        #    its non-sub: turns; a nested builder records the turns of the subagent it
        #    represents (agent_id == own_agent_id). A registered child's turn never moves it.
        if etype == "llm_response":
            usage = payload.get("usage")
            # claude_code's coalesced usage is streaming-partial; its authoritative
            # totals come from query_result, not from summing llm_response usage.
            if payload.get("provider") != "claude_code":
                self._tokens.add_usage(usage)
            pt = usage_prompt_tokens(usage)
            is_subagent = isinstance(agent_id, str) and agent_id.startswith("sub:")
            # pt==0 is an all-zero "empty reply" (e.g. freshly-resumed
            # claude_code) — no gauge signal, hold last good values. pt is None
            # (usage absent) is distinct: context_pct/window may still update.
            if pt != 0 and (not is_subagent or agent_id == self._own_agent_id):
                if pt is not None:
                    self._latest_prompt_tokens = pt
                cpct = payload.get("context_pct")
                if cpct is not None:
                    self._latest_context_pct = cpct
                cwin = payload.get("context_window")
                if cwin is not None:
                    self._latest_context_window = cwin
            # Separate from the top-level gauge above (which drops subagents):
            # remember this turn's occupancy regardless of agent, so a following
            # reincarnating event can name the context that crossed the checkpoint.
            ctoks = payload.get("context_tokens")
            cwin_any = payload.get("context_window")
            if ctoks is not None and cwin_any:
                self._last_llm_context = (ctoks, cwin_any)
        elif etype == "query_result":
            # Authoritative per-query totals/cost from claude_code. Accumulated
            # BEFORE sub-routing so a subagent's query_result still folds in.
            self._tokens.add_usage(payload.get("usage"))
            self._tokens.cost += float(payload.get("total_cost_usd") or 0)

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
            is_task_call = etype == "tool_call" and payload.get("name") == "sub_agent"
            is_parent_mid_turn = (
                etype == "thinking"
                or (etype == "llm_response"
                    and not _llm_response_is_turn_end(payload)))
            matched = (
                etype == "tool_result"
                and (rid_ in self._open_sub_by_block or rid_ in self._open_sub_by_event))
            if not matched and not is_task_call and not is_parent_mid_turn:
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

    def _flush_warnings_summary(self) -> None:
        # Append ONE WarningsSummaryNode at the workflow root (bypassing _append so
        # it lands at root regardless of open phases/steps) just before a terminal
        # outcome node. Idempotent: a second call is a no-op, so co-occurring
        # terminals still render the summary once. No-op when no warnings occurred.
        if not self._warnings or self._warnings_flushed:
            return
        counts: dict[str, int] = {}
        order: list[str] = []
        for msg in self._warnings:
            if msg not in counts:
                counts[msg] = 0
                order.append(msg)
            counts[msg] += 1
        self._root.children.append(WarningsSummaryNode(
            lines=[(msg, counts[msg]) for msg in order],
            total=len(self._warnings)))
        self._warnings_flushed = True

    def finalize(self) -> None:
        """End-of-stream flush of a deferred rename and the warnings summary on a pause."""
        self._flush_pending_rename()
        if self._root.status == "paused" and self._pause_outcome_shown:
            self._flush_warnings_summary()

    def _flush_burst(self) -> None:
        self._current_burst = None

    def _flush_pending_rename(self) -> None:
        # Append the deferred rename here, not in _flush_burst (which runs before the narration).
        if self._pending_rename is None:
            return
        self._append(self._pending_rename)
        self._pending_rename = None

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
    b.finalize()
    return b.tree


# Handlers populate this in subsequent tasks; change_created lives here so the
# skeleton test passes.
def _h_change_created(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    if p.get("kind") == "backlog":
        return
    self._root.title = p.get("title", "")
    self._root.id = p.get("id")


NarrativeBuilder._HANDLERS["change_created"] = _h_change_created


def _h_change_renamed(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # A rename carries the new title. Apply it; ignore a legacy/partial event that
    # is missing new_title. The id is immutable; the breadcrumb shows the new title.
    new_title = p.get("new_title")
    if new_title is None:
        return
    self._root.title = new_title
    # Stash the rename; flushed after the narration or at the next boundary; latest title wins.
    self._pending_rename = RenameNode(name=new_title)


NarrativeBuilder._HANDLERS["change_renamed"] = _h_change_renamed


def _h_change_merged(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_pending_rename()
    self._flush_burst()
    self._root.status = "completed"
    descriptor = self._root.id or ""
    # Append at the WORKFLOW ROOT (not the still-open phase): the merged footer is a
    # run-level terminal line that ends the whole transcript, after every phase.
    self._flush_warnings_summary()
    self._root.children.append(MergedNode(change_id=descriptor, base=str(p.get("base", ""))))


NarrativeBuilder._HANDLERS["change_merged"] = _h_change_merged


def _h_workflow_completed(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_pending_rename()
    self._flush_burst()
    self._root.status = "completed"
    descriptor = self._root.id or ""
    # Append at the WORKFLOW ROOT: a run-level terminal line ending the whole
    # transcript, after every phase.
    self._flush_warnings_summary()
    self._root.children.append(CompletedNode(change_id=descriptor))


NarrativeBuilder._HANDLERS["workflow_completed"] = _h_workflow_completed


def _h_merge_blocked(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # _emit_merge_blocked already renders the friendly ⚠ block; suppress the raw fallthrough.
    self._flush_pending_rename()
    self._flush_burst()


NarrativeBuilder._HANDLERS["merge_blocked"] = _h_merge_blocked


def _h_merge_retry(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_burst()
    attempt = p.get("attempt", 0)
    cap = p.get("max", 0)
    self._append(LiteralTextNode(
        text=f"retrying integration (attempt {attempt}/{cap})",
        color_role="lifecycle"))


NarrativeBuilder._HANDLERS["merge_retry"] = _h_merge_retry


def _h_lane_waiting(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_burst()
    text = f"waiting to merge · {p.get('ahead', 0)} ahead"
    step = self._step_stack[-1] if self._step_stack else None
    if step is not None and step.lane_waiting_node is not None:
        step.lane_waiting_node.text = text
    else:
        node = LiteralTextNode(text=text, color_role="lifecycle")
        self._append(node)
        if step is not None:
            step.lane_waiting_node = node
    if step is not None:
        step.live_label = text
        step.lane_wait = True


NarrativeBuilder._HANDLERS["lane_waiting"] = _h_lane_waiting


def _h_waiting_for_test_lane(
    self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]
) -> None:
    self._flush_burst()
    # Lane-waits are live-footer-only: no scrollback node. The step's `started`
    # already fired, so current_activity() reads its "running tests" phrase —
    # override the footer verb with "waiting to test" until test_lane_acquired clears it.
    if self._step_stack:
        self._step_stack[-1].live_label = "waiting to test"
        self._step_stack[-1].lane_wait = True


NarrativeBuilder._HANDLERS["waiting_for_test_lane"] = _h_waiting_for_test_lane


def _h_test_lane_acquired(
    self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]
) -> None:
    self._flush_burst()
    # Slot in hand — drop the "waiting to test" override so the footer reverts to the
    # step's phrase ("running tests"), and store the acquisition time in run_start_ts
    # (the run clock the breadcrumb reads), leaving start_ts intact so phase_elapsed
    # still measures the phase's full wall-clock span including the queue wait.
    if self._step_stack:
        top = self._step_stack[-1]
        top.live_label = None
        top.lane_wait = False
        ts = event.get("timestamp")
        if ts is not None:
            top.run_start_ts = ts


NarrativeBuilder._HANDLERS["test_lane_acquired"] = _h_test_lane_acquired


def _fmt_ctx_tokens(n: int) -> str:
    """Compact token count for the reincarnating line: '167k' at/above 1k, else bare."""
    return f"{round(n / 1000)}k" if n >= 1000 else str(n)


def _h_reincarnating(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_burst()
    reason = p.get("reason")
    text = f"reincarnating — {reason}" if reason else "reincarnating"
    # Name the occupancy that crossed the checkpoint (tokens/window), so the line
    # is self-explaining and the window is unambiguous — no re-deriving it from
    # cumulative token counters, which read far larger than the live context.
    ctx = self._last_llm_context
    if ctx is not None and ctx[1]:
        text += f" ({_fmt_ctx_tokens(ctx[0])}/{_fmt_ctx_tokens(ctx[1])})"
    self._append(LiteralTextNode(text=text, color_role="lifecycle"))


NarrativeBuilder._HANDLERS["reincarnating"] = _h_reincarnating


def _h_lane_event(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_burst()


def _h_lane_acquired(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_burst()
    if self._step_stack:
        top = self._step_stack[-1]
        top.live_label = None
        top.lane_waiting_node = None
        top.lane_wait = False


NarrativeBuilder._HANDLERS["lane_acquired"] = _h_lane_acquired
NarrativeBuilder._HANDLERS["lane_released"] = _h_lane_event
NarrativeBuilder._HANDLERS["lane_evicted"] = _h_lane_event


def _ensure_phase(self: NarrativeBuilder, name: str,
                  phase_label: str | None = None, show_label: bool = True) -> None:
    """Derive the phase grouping from step_started.payload.phase. When the phase
    name changes from the current open one, lazily create a PhaseNode, append it
    to the workflow root, and make it the current scope. There are no
    phase_started/phase_completed events — phase is purely DERIVED here."""
    # A dropped step opener leaves prose as held AgentTextNodes under the workflow
    # root, where no step_completed can un-hold them — stalling the emit cursor.
    for c in self._root.children:
        if isinstance(c, AgentTextNode) and c.held:
            c.held = False
    if (self._current_phase is not None and not self._current_phase.closed
            and self._current_phase.name == name):
        return
    if self._current_phase is not None:
        self._current_phase.closed = True   # let the live cursor pop the drained phase
    phase = PhaseNode(name=name, label=phase_label, show_label=show_label)
    self._root.children.append(phase)
    self._current_phase = phase
    self._step_stack.clear()


def _h_step_started(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_pending_rename()
    self._flush_burst()
    self._span_start_ts = event.get("timestamp")
    self._segment_had_work = False
    name = p.get("step", "?")
    kind = p.get("kind")
    phase = p.get("phase", "?")
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
            if p.get("resumed"):
                if p.get("restarted"):
                    top.live_label = None
                top.lane_waiting_node = None
                top.label = p.get("label")
                top.resumed = True
            return
    # Resume re-entry: the first step_started after a resume re-enters the
    # interrupted step in the still-open phase. `restarted` (always present on a
    # resume) tells the two cases apart: True is a genuine re-run from an
    # empty/never-ran transcript — wipe the partial and restamp the clock; False
    # is a continuation (the transcript is replayed and continued) — keep the
    # pre-interrupt children and the original clock, only reopening the node for
    # appending. Consume the re-entry window regardless of whether a match is found.
    resuming, self._resuming = self._resuming, False
    if (resuming and self._current_phase is not None
            and not self._current_phase.closed
            and self._current_phase.name == phase):
        existing = next(
            (c for c in self._current_phase.children
             if isinstance(c, StepNode) and c.name == name and not c.closed), None)
        if existing is not None:
            # Shared reopen — applies to a re-run AND a continuation: reopen the
            # node for appending and refresh the resume-display fields.
            existing.outcome = None
            existing.closed = False
            existing.interrupted = False
            existing.label = p.get("label")
            existing.lane_waiting_node = None
            existing.resumed = True
            if p.get("restarted"):
                # Genuine re-run from scratch: the fresh run supersedes the
                # interrupted partial, so wipe it, restamp the clock, and reset
                # the re-run-only fields. Interactivity is re-sourced from the
                # event stream (a re-emitted awaiting_user_input re-arms it).
                existing.live_label = None
                existing.children.clear()
                existing.kind = kind
                existing.model = p.get("model")
                existing.provider = p.get("provider")
                existing.interactive = False
                existing.start_ts = event.get("timestamp")
                existing.run_start_ts = None
                existing.agent_output = None
                existing.command = p.get("command")
                existing.show_label = bool(p.get("show_label", True))
                existing.show_output = bool(p.get("show_output", False))
                existing.phase_label = p.get("phase_label")
                existing.route_label = p.get("route_label")
                existing.rotate_phrase = bool(p.get("rotate_phrase", False))
                existing.position_label = bool(p.get("position_label", False))
                existing.rotated_phrase = (
                    _draw_status_phrase() if existing.rotate_phrase else None)
            self._step_stack = [existing]
            return
    _ensure_phase(self, phase, p.get("phase_label"),
                  p.get("phase_show_label", True))
    # interactive defaults False — it is re-sourced from the event stream (an
    # awaiting_user_input event marks the step interactive); step_started no
    # longer carries an interactive key.
    node = StepNode(name=name, kind=kind, model=p.get("model"),
                    provider=p.get("provider"),
                    start_ts=event.get("timestamp"),
                    show_label=bool(p.get("show_label", True)),
                    show_output=bool(p.get("show_output", False)),
                    position_label=bool(p.get("position_label", False)),
                    label=p.get("label"),
                    phase_label=p.get("phase_label"),
                    command=p.get("command"),
                    resumed=bool(p.get("resumed", False)),
                    route_label=p.get("route_label"),
                    rotate_phrase=bool(p.get("rotate_phrase", False)))
    self._append(node)
    self._step_stack.append(node)
    if node.rotate_phrase:
        node.rotated_phrase = _draw_status_phrase()


def _h_step_completed(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_pending_rename()
    self._flush_burst()
    # D1: an awaiting-reply arm is valid only within the step that raised it — a
    # solicit is always answered before its step ends. If the arm was never
    # consumed by a human user_message (e.g. the run paused at the gate and
    # resumed with an engine-continuation nudge, which is not a user_message), it
    # must not survive the step boundary; otherwise the next step's begin-kickoff
    # user_message is mis-tagged interactive and leaks as a `> ...` human turn in
    # live stream and imp log replay. Disarm at completion (not at step start, so
    # a resume-interject reply delivered on re-entry is still classified human).
    self._awaiting_reply = False
    self._close_open_folds_at_step_end(event.get("timestamp"))
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
        elapsed = elapsed_seconds(node.run_start_ts or node.start_ts, event.get("timestamp"))
        if elapsed is not None:
            elapsed = max(0.0, elapsed - node.backoff_wait_s)
        breadcrumb = BreadcrumbLine(
            step_name=node.name, kind=node.kind, elapsed_s=elapsed, action=action,
            extras=_breadcrumb_extras(node.agent_output),
            step_label=node.label,
            label=p.get("label"),
            phase_label=node.phase_label,
            interrupted=node.interrupted)
        node.children.append(breadcrumb)
        node.closed = True


NarrativeBuilder._HANDLERS["step_started"] = _h_step_started
NarrativeBuilder._HANDLERS["step_completed"] = _h_step_completed


def _h_step_transition(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # Backward transition (engine-declared): the source step was already popped,
    # so _append lands the marker in the open phase (or root) — the bounce point.
    self._flush_pending_rename()
    self._flush_burst()
    frm = p.get("from_step", "?")
    to = p.get("target_step", "?")
    reason = p.get("reason")
    text = f"↩ {frm} → {to}"
    if reason:
        text += f"  ({reason})"
    self._append(LiteralTextNode(text=text, color_role="lifecycle"))


NarrativeBuilder._HANDLERS["step_transition"] = _h_step_transition


def _h_workflow_paused(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # EVERY pause is a pause, not an error — a resumable stop. Live frames them all
    # the same way (_emit_paused_summary prints a dim `paused`),
    # so `imp log` renders a matching PauseNode rather than an `error:` line. The
    # reason that drove it (ctrl-c, api, max_loops, retry_ceiling, …) appears on the
    # line; for an error-driven pause the underlying failure was already shown as
    # its own `error:` event just above, so re-stating it as an error here would
    # both double up and mismatch the live view (the bug this fixes).
    self._flush_pending_rename()
    self._flush_burst()
    self._root.status = "paused"
    reason = p.get("interrupt_reason") or p.get("reason")
    if reason:
        # Unblock the live DFS walk to the nested pause; narrower than the resume
        # loop (no membership/salvage mutation).
        for fold in self._open_sub_by_event.values():
            fold.running = False
            fold.terminal_state = "interrupted"
        # A genuine your-turn yield is a normal solicit, not an interrupt: the
        # agent's prose and the eventual reply already carry it, so the pause
        # line is redundant. Finalize folds and mark paused above, but append no
        # PauseNode. (Shared render path — covers both imp log and the live view.)
        if reason == "awaiting_user":
            return
        # Anchor the pause in the innermost open scope so it renders where it fired.
        self._append(PauseNode(reason=reason))
        self._pause_outcome_shown = True


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
        # Remember it: a task whose dispatch outlived the pause may still log its
        # canonical result after resume; recognise it then instead of warning.
        self._remember_salvage_closed(fold)
    self._open_sub_by_block.clear()
    self._open_sub_by_event.clear()
    self._resuming = True                # open the re-entry window
    self._warnings_flushed = False       # let the next terminal flush a fresh summary
    self._pause_outcome_shown = False    # a resumed run is no longer a reasoned pause


NarrativeBuilder._HANDLERS["workflow_paused"] = _h_workflow_paused
NarrativeBuilder._HANDLERS["workflow_resumed"] = _h_workflow_resumed


def _h_cli_attached(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    pass


NarrativeBuilder._HANDLERS["cli_attached"] = _h_cli_attached


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
    # Un-hold any held narration in the current container: the prose carried by
    # the llm_response context the human must read to answer, and the agent is now blocked
    # settled, NOT a trailing narration that could still fold into a step
    # conclusion. Without this the live _walk blocks on the held node
    # (_is_final is False for a held AgentTextNode) and the message surfaces only
    # after the user replies (the next llm_response un-holds it). Mirrors the
    # un-hold-all loop in _h_llm_response.
    for c in self._container().children:
        if isinstance(c, AgentTextNode) and c.held:
            c.held = False
    return None


def _h_user_interjected(
    self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any],
) -> None:
    # A human interject is delivered UNPROMPTED (no preceding awaiting_user_input),
    # so deliver_interject emits user_message THEN user_interjected (that emit order
    # is a deliberate prior decision — keep it). The user_message just appended a
    # UserMessageNode with interactive=False, which render_text would drop as a
    # synthetic engine prompt. Retroactively mark that node interactive=True so it
    # renders as a genuine `> …` human turn. The node is the most-recently-appended child of
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


def is_prose_field(key: str, value: Any) -> bool:
    """True for a free-text output field: excluded from the breadcrumb and surfaced under --full."""
    return (isinstance(value, str) and not key.endswith("_path")
            and ("\n" in value or len(value) > _PROSE_THRESHOLD))



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
        if is_prose_field(k, v):
            continue
        if isinstance(v, (str, int, float, bool)):
            out.append(f"{k}={v}")
    return out


def phase_elapsed(phase: PhaseNode) -> float | None:
    """Phase duration derived from its steps: first step start_ts → last step
    end_ts. No phase_started/phase_completed events exist (§7) — phase timing is
    derived. None when the phase has no timestamped steps."""
    steps = [c for c in phase.children if isinstance(c, StepNode)]
    if not steps:
        return None
    return elapsed_seconds(steps[0].start_ts, steps[-1].end_ts)


def elapsed_seconds(start_ts: str | None, end_ts: Any) -> float | None:
    """Diff two ISO-8601 timestamps (state.py writes datetime.now(UTC).isoformat()).
    Returns None when either endpoint is missing or unparseable (issue 3)."""
    if not start_ts or not isinstance(end_ts, str) or not end_ts:
        return None
    try:
        return (datetime.fromisoformat(end_ts)
                - datetime.fromisoformat(start_ts)).total_seconds()
    except (ValueError, TypeError):
        return None


def _draw_status_phrase() -> str:
    # Lazy import keeps `import narrative` free of agent-package weight (design D3).
    from imp.agents.random_status import random_status_phrase
    return random_status_phrase()


def _rotate_status_phrase(self: NarrativeBuilder) -> None:
    """Draw a fresh phrase onto the open rotating step at a segment boundary if
    work occurred; always consumes the segment-work flag."""
    had_work, self._segment_had_work = self._segment_had_work, False
    if had_work and self._step_stack and self._step_stack[-1].rotate_phrase:
        self._step_stack[-1].rotated_phrase = _draw_status_phrase()


def _h_user_message(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_burst()
    _rotate_status_phrase(self)
    self._span_start_ts = event.get("timestamp")
    interactive, self._awaiting_reply = self._awaiting_reply, False
    text = _user_message_text(p)
    if interactive and not text.strip():
        block = _sole_success_tool_result(p)
        if block is not None:
            # Live-safe resume reply: the injected message's sole block is a
            # success tool_result carrying the human's reply (anthropic/openai/
            # claude_code resume shape).
            text = _summarize_result(block.get("content"))
    pending, self._pending_reply_node = self._pending_reply_node, None
    if pending is not None:
        # Reconcile the optimistic echo: adopt authoritative text in place
        # (one-to-one awaiting-reply match).
        pending.text = text
        pending.interactive = interactive
        return
    self._append(UserMessageNode(text=text, interactive=interactive))


def _h_llm_response(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # Token accumulation already happened in add(); here we only build the node.
    # §8: an empty-text llm_response is a tool-use-turn carrier (a lone tool_use
    # block). It must NOT flush an open burst — otherwise consecutive tool calls
    # each start a fresh burst and the status counter sticks at 1. Gate on the
    # shared prose predicate.
    if not llm_response_has_prose(p):
        return
    node = AgentTextNode(payload=p)
    # A prose turn after a completion_nudge clears the flag; a bare WAITING
    # protocol reply (case-insensitive) is dropped before any side effect.
    if self._completion_nudge_pending:
        self._completion_nudge_pending = False
        if node.text.strip().casefold() == "waiting":
            return
    _rotate_status_phrase(self)
    if self._current_burst is not None:
        self._current_burst.span_end_ts = event.get("timestamp")
    self._flush_burst()
    self._span_start_ts = event.get("timestamp")
    # A new narration is taking its place, so every EARLIER narration in this
    # container is now settled (only the TRAILING one can still fold into the
    # step conclusion). Un-hold them ALL — not just the immediately-adjacent
    # one. The old `_last_narration_index`-only un-hold silently skipped any
    # narration separated from the newcomer by a ThinkingFold / UserMessageNode
    # (i.e. a normal tool/think turn — `_last_narration_index` only scans past
    # ToolBurstFold), so interleaved prose left permanently-held nodes. In the
    # live view a held node blocks the emit cursor, so a single stuck-held
    # opening narration froze the whole run at its first step.
    children = self._container().children
    for c in children:
        if isinstance(c, AgentTextNode) and c.held:
            c.held = False
    node.held = True
    self._append(node)
    # Re-attach the deferred rename after its narration; not in _flush_burst, which ran earlier.
    self._flush_pending_rename()


def _h_thinking(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._segment_had_work = True
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
    # args on the open StepNode: scalars fold into the breadcrumb, the full args
    # surface only under `--full`. A tool call's arguments are plumbing, never a
    # prose block — the agent's narration is the single rendered copy (E1).
    if not self._step_stack:
        return
    self._flush_burst()
    step = self._step_stack[-1]
    step.agent_output = p
    # Un-hold the agent's final narration so the live driver emits it now: the
    # agent has yielded, so its trailing prose is settled, not a held tail.
    children = self._container().children
    narration_idx = _last_narration_index(children)
    if narration_idx is not None:
        children[narration_idx].held = False


def _h_opening_intent(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    text = p.get("text", "")
    if self._opening_intent_node is not None:
        # Seeded optimistically: adopt the authoritative text in place; never append a duplicate.
        self._opening_intent_node.text = text
        return
    node = UserMessageNode(text=text, interactive=True)
    self._opening_intent_node = node
    self._append(node)


def _h_query_result(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # Token totals + cost are accumulated in add() (pre-routing, so subagent
    # query_results fold into the change total). No visible node.
    return


NarrativeBuilder._HANDLERS["opening_intent"] = _h_opening_intent
NarrativeBuilder._HANDLERS["user_message"] = _h_user_message
NarrativeBuilder._HANDLERS["llm_response"] = _h_llm_response
NarrativeBuilder._HANDLERS["thinking"] = _h_thinking
NarrativeBuilder._HANDLERS["step_context"] = _h_step_context
NarrativeBuilder._HANDLERS["agent_output"] = _h_agent_output
NarrativeBuilder._HANDLERS["query_result"] = _h_query_result


def _ensure_burst(self: NarrativeBuilder) -> ToolBurstFold:
    if self._current_burst is None:
        self._current_burst = ToolBurstFold(span_start_ts=self._span_start_ts)
        self._append(self._current_burst)
    return self._current_burst


def _h_tool_call(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._segment_had_work = True
    if p.get("name") == "sub_agent":
        _open_subagent_fold(self, event, p)     # defined in Task 6
        return
    burst = _ensure_burst(self)
    if burst.start_ts is None:
        burst.start_ts = event.get("timestamp")  # burst clock starts at first call
    call = ToolCall(
        call_id=str(p.get("id", "")), name=str(p.get("name", "tool")),
        input=p.get("input") or {}, call_ts=event.get("timestamp"))
    burst.calls.append(call)


def _close_subagent_fold(fold: SubagentFold, p: dict[str, Any],
                         end_ts: str | None = None) -> None:
    fold.running = False
    fold.end_ts = end_ts
    fold.is_error = bool(p.get("error", False))
    fold.outcome = _summarize_result(p.get("content"))
    # Terminal state for Part D's glyph. Prefer an explicit payload kind (the
    # claude_code degraded-pause path sets kind="interrupted"); else detect the
    # bracketed marker an is_error failure message embeds ("... [stalled]" /
    # "... [interrupted]" — the codex degraded-pause path appends the latter).
    # The brackets keep the match precise (low false-positive).
    kind = p.get("kind")
    if kind not in ("stalled", "interrupted", "context_exhausted"):
        # The bracketed marker only originates from the failure path; a
        # SUCCESSFUL subagent's content is agent-controlled JSON that may
        # legitimately contain "[stalled]". Gate the scan on is_error so such
        # text can't false-positive into a terminal state.
        text = fold.outcome if fold.is_error else ""
        _kinds = ("stalled", "interrupted", "context_exhausted")
        kind = next((s for s in _kinds if f"[{s}]" in (text or "")), None)
    if kind in ("stalled", "interrupted", "context_exhausted"):
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


def _take_closed_fold(
    self: NarrativeBuilder, rid: str, id_ns: Any,
) -> SubagentFold | None:
    """Probe the SALVAGE-closed registry for rid in the id_ns namespace; remove it
    so a late canonical result is applied at most once. Stamped namespaces only —
    an unstamped legacy result never reaches this path (it has its own probe)."""
    if id_ns == "event":
        fold = self._closed_sub_by_event.get(rid)
    elif id_ns == "block":
        fold = self._closed_sub_by_block.get(rid)
    else:
        return None
    if fold is not None:
        self._closed_sub_by_block.pop(fold.block_id, None)
        self._closed_sub_by_event.pop(fold.event_id, None)
    return fold


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
        return
    if status == "stamped_miss":
        # Before warning: is this the AUTHORITATIVE result for a fold a salvage
        # path already closed provisionally (lost-result fallback / pause-resume
        # finalize)? If so it's a benign race, not a malformed log — apply the
        # real outcome to the already-closed fold and stay quiet.
        late = _take_closed_fold(self, rid, p.get("id_ns"))
        if late is not None:
            _close_subagent_fold(late, p, ts)
            return
        self._append(ErrorNode(
            message=f"task result {rid!r} (id_ns={p.get('id_ns')!r}) matched no open fold",
            is_warning=True))
        return

    # Normal burst result: pair with the matching open call.
    burst = self._current_burst
    if burst is not None:
        for call in burst.calls:
            if call.call_id == rid and call.result is None:
                # This turn actually ran → advance the clock.
                burst.end_ts = ts
                call.result = summary
                call.citations_footer = footer
                call.is_error = is_error
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



NarrativeBuilder._HANDLERS["tool_call"] = _h_tool_call
NarrativeBuilder._HANDLERS["tool_result"] = _h_tool_result
NarrativeBuilder._HANDLERS["tool_killed"] = _h_tool_killed


def _h_skill_load(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # The agent pulled in a skill. Render it once, as its own `loading skill …`
    # line; the matching `load_skill` tool call is dropped from the burst recap
    # (render_text) so the same action doesn't show up twice. Do NOT flush the
    # open burst: this event fires BETWEEN a tool_call and its tool_result (the
    # load_skill round-trip), so flushing would orphan that pending result into a
    # stray `tool` burst.
    self._append(SkillLoadNode(name=str(p.get("name", "?")), source=p.get("source")))


NarrativeBuilder._HANDLERS["skill_load"] = _h_skill_load


def _h_step_label_updated(
    self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]
) -> None:
    # Live status-bar label for the current step. Do NOT flush the open burst:
    # this fires between the update_step_label tool_call and its tool_result.
    if self._step_stack:
        self._step_stack[-1].live_label = p.get("label")


NarrativeBuilder._HANDLERS["step_label_updated"] = _h_step_label_updated


def _h_error(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_burst()
    if p.get("category") == "config" and p.get("kind") == "interrupted":
        if self._step_stack:
            self._step_stack[-1].interrupted = True
        return
    if p.get("category") == "stalled":
        # Calm stall teardown warning; lazy import keeps narrative.py module-level-import-free.
        from imp.agents.tools.limits import AGENT_INACTIVITY_TIMEOUT_S
        self._append(ErrorNode(
            message=(f"no response for {AGENT_INACTIVITY_TIMEOUT_S:.0f}s"
                     " — resuming; work already saved is preserved"),
            category="stalled", is_warning=True))
        return
    from imp.engine.error_policy import is_retry_with_backoff
    if is_retry_with_backoff(str(p.get("category") or "")):
        # Transient backoff-retry error: suppress the red line and drop the held auth
        # prose via a stripped PREFIX match (last_agent_text is stored [:500]).
        d = p.get("detail")
        last = str(d["last_agent_text"]) if isinstance(d, dict) and d.get("last_agent_text") else ""
        target = last.strip()
        if target:
            children = self._container().children
            # A deferred rename flushed right after the held narration (see
            # _flush_pending_rename in _h_llm_response) leaves a trailing
            # RenameNode; skip it so the held-prose lookup still lands on the
            # narration. Trimming only the tail keeps indices aligned for del.
            end = len(children)
            if end and isinstance(children[end - 1], RenameNode):
                end -= 1
            idx = _last_narration_index(children[:end])
            if (idx is not None and children[idx].held
                    and children[idx].text.strip().startswith(target)):
                del children[idx]
        return
    detail = ""
    d = p.get("detail")
    if isinstance(d, dict):
        if d.get("full_summary"):
            detail = str(d["full_summary"])
        elif d.get("stderr_tail"):
            tail = str(d["stderr_tail"])
            exit_code = d.get("exit_code")
            detail = (
                f"exit code {exit_code}\n{tail}"
                if isinstance(exit_code, int)
                else tail
            )
        elif d.get("last_agent_text"):
            detail = str(d["last_agent_text"])
    self._append(ErrorNode(
        message=str(p.get("message", "?")), category=p.get("category"),
        is_warning=False, detail=detail))


def _h_runner_warning(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # Non-terminal: do NOT flush the burst (a warning can be emitted mid-burst
    # for a dropped block; flushing would split an otherwise-contiguous run).
    message = str(p.get("message", "?"))
    self._warnings.append(message)
    self._append(ErrorNode(message=message, is_warning=True))


def _h_warning(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # Non-terminal: does NOT flush the burst (same invariant as _h_runner_warning).
    message = str(p.get("message", "?"))
    self._warnings.append(message)
    self._append(ErrorNode(message=message, is_warning=True))


def _fmt_hm(seconds: float) -> str:
    """`2h 13m` / `45m` compact duration; hours segment shown only when non-zero."""
    total_minutes = int(seconds // 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


_RETRY_VERB: dict[str, str] = {
    "rate_limit": "waiting for limit reset",
    "auth": "waiting for re-authentication",
    "network": "network - waiting to retry",
    "stalled": "stalled - waiting to retry",
}


def _retry_verb(category: str) -> str:
    """Wait verb for a retry-with-backoff category; unnamed ones get the generic verb."""
    return _RETRY_VERB.get(category, "waiting to retry")


def _h_agent_retry(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # Non-terminal: a transient error was retried with backoff. Render inline so
    # a self-healed network/rate_limit blip doesn't look like a failure.
    cat = p.get("category", "?")
    # lazy import keeps narrative.py module-level-import-free.
    from imp.engine.error_policy import is_retry_with_backoff
    backoff = is_retry_with_backoff(str(cat))
    if backoff and self._backoff_wait_start is None:
        self._backoff_wait_start = event.get("timestamp")
    attempt = p.get("attempt", "?")
    secs = p.get("backoff_seconds", "?")
    # Wait state is uniform per backoff category; only the breadcrumb stays category-specific.
    if backoff and isinstance(secs, (int, float)):
        self._reset_wait = ResetWait(verb=_retry_verb(str(cat)),
                                     total_seconds=float(secs))
    if cat == "stalled":
        # Stalled retry is the calm resume re-entry breadcrumb, not a technical retry line.
        self._append(ErrorNode(
            message="resuming — verifying prior work, not redoing it",
            category=cat, is_warning=True))
        return
    hinted = bool(p.get("hinted")) or (isinstance(secs, (int, float)) and secs >= 120)
    if hinted and isinstance(secs, (int, float)):
        self._append(ErrorNode(
            message=f"[{cat}] waiting for limit reset — retry {attempt} in {_fmt_hm(secs)}",
            category=cat, is_warning=True))
        return
    self._append(ErrorNode(
        message=f"[{cat}] retry {attempt} ({secs}s)", category=cat, is_warning=True))


def _h_output_validation_nudge(
    self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]
) -> None:
    # Render inline as a warning so a malformed complete_step that was nudged and
    # recovered in-session doesn't look like a failure (mirrors agent_retry).
    reason = str(p.get("message", "output validation"))
    attempt = p.get("attempt", "?")
    self._append(ErrorNode(
        message=f"malformed complete_step, nudged (attempt {attempt}): {reason}",
        is_warning=True))


def _h_completion_nudge(
    self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]
) -> None:
    # Telemetry-only (events.jsonl keeps the forensic record; render nothing).
    # Arm the flag so the next prose llm_response can drop a bare WAITING reply.
    self._completion_nudge_pending = True


def _h_script_blocked(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_burst()
    step = str(p.get("step", "?"))
    code = p.get("exit")
    suffix = f" (exit {code})" if isinstance(code, int) else ""
    stderr = str(p.get("stderr", "")).strip()
    stdout = str(p.get("stdout", "")).strip()
    detail = stderr
    if stdout:
        detail = f"{detail}\n{stdout}" if detail else stdout
    detail = _truncate(detail)
    self._append(ErrorNode(
        message=f"{step} blocked{suffix}", category="script",
        is_warning=False, detail=detail))


def _h_max_tokens_truncated(
    self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]
) -> None:
    # Telemetry-only; the sibling runner_warning is the visible signal (no breadcrumb).
    return


def _h_engine_continuation(
    self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]
) -> None:
    # Internal retry self-continuation; suppressed live (human sees it via agent_retry).
    return


NarrativeBuilder._HANDLERS["error"] = _h_error
NarrativeBuilder._HANDLERS["runner_warning"] = _h_runner_warning
NarrativeBuilder._HANDLERS["warning"] = _h_warning
NarrativeBuilder._HANDLERS["agent_retry"] = _h_agent_retry
NarrativeBuilder._HANDLERS["output_validation_nudge"] = _h_output_validation_nudge
NarrativeBuilder._HANDLERS["completion_nudge"] = _h_completion_nudge
NarrativeBuilder._HANDLERS["script_blocked"] = _h_script_blocked
NarrativeBuilder._HANDLERS["max_tokens_truncated"] = _h_max_tokens_truncated
NarrativeBuilder._HANDLERS["engine_continuation"] = _h_engine_continuation


def _h_script_output(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_burst()
    self._append(LiteralTextNode(text=_truncate(str(p.get("stdout", ""))),
                                 color_role=None, verbatim=False))


NarrativeBuilder._HANDLERS["script_output"] = _h_script_output


def _h_tiers_resolved(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # Fires once at run start with agent_id=None, BEFORE any step_started, so the
    # step stack is empty and _append lands this at the workflow root.
    # Payload: {"tiers": {tier: {provider, model, ...}}, "steps": {step: {...}},
    #           "checkpoint_threshold": ...}
    self._latest_checkpoint_threshold = p.get("checkpoint_threshold")
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
    declares `task` in its tools; its subagents (coder, reviewers) declare
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
        event_id=event_id, block_id=block_id,
        nested=NarrativeBuilder(own_agent_id=f"sub:{event_id}"),
        opener_agent_id=agent_id if isinstance(agent_id, str) else None,
        start_ts=event.get("timestamp"),
        awaits_result=p.get("id_ns") == "block",
        label=p.get("label"),
        task_description=str(inp["task_description"]) if inp.get("task_description") else None)
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
        return {"t": t, "id": node.id, "title": node.title,
                "status": node.status,
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
        return {"t": t, "text": node.text, "interactive": node.interactive}
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
    if isinstance(node, BreadcrumbLine):
        return {"t": t, "step_name": node.step_name, "elapsed_s": node.elapsed_s,
                "action": node.action, "extras": node.extras}
    if isinstance(node, FallbackLine):
        return {"t": t, "event_type": node.event_type, "payload": node.payload}
    if isinstance(node, MergedNode):
        return {"t": t, "change_id": node.change_id, "base": node.base}
    if isinstance(node, CompletedNode):
        return {"t": t, "change_id": node.change_id}
    return {"t": t}

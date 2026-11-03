# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
# src/imp/cli/narrative.py
"""Narrative-tree model for imp's unified renderer.

Folds a stream of event dicts (events.jsonl shape) into an ordered tree of
typed nodes. Fed events ONE AT A TIME via NarrativeBuilder.add(), so the live
on_event path and the batch replay path (build_tree) share exactly one code
path and cannot drift (design §5.4).

PURITY (design D3): no module-level third-party imports. The one
provider-coupled operation — turning an agent_message.raw payload into assistant
text — is a LAZY import of imp.cli.render_helpers inside AgentTextNode.text,
exactly as today's render.py lazy-imports it. So `import narrative` is cheap and
tree-structure tests pull nothing heavy.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from imp.engine.feedback import truncate_detail

if TYPE_CHECKING:
    from imp.providers.quota import QuotaReading

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

# Human interrupt/leave reasons: like the silent yield, they append no PauseNode.
_HUMAN_INTERRUPT_REASONS: frozenset[str] = frozenset(
    {"ctrl-c", "ctrl-d", "sigterm", "sighup", "sigquit", "pause"})


def _agent_message_is_turn_end(payload: dict[str, Any]) -> bool:
    # load-bearing: codex None = turn-end; claude None = mid-stream fragment
    provider = payload.get("provider")
    if provider == "openai":
        return payload.get("stop_reason") in _OPENAI_TERMINAL_STATUSES
    if provider == "codex":
        return True
    return payload.get("stop_reason") in _TURN_END_STOP_REASONS


def agent_message_has_prose(payload: dict[str, Any]) -> bool:
    """True when an agent_message carries a visible prose block. A lone tool_use
    carrier has empty extracted text and returns False."""
    return bool(AgentTextNode(payload=payload).text.strip())


def usage_prompt_tokens(usage: dict[str, Any] | None) -> int | None:
    """One turn's BILLING prompt sum (input + both cache buckets); equals live occupancy
    only on the anthropic/openai push paths (single model request), not on codex.
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
    lane_wait_start_ts: str | None = None  # lane queue-entry clock (block-age)
    end_ts: str | None = None          # ISO-8601 from step_completed.timestamp (§7)
    round: int = 1                     # per-loop entry count from step_started
    agent_result: dict[str, Any] | None = None  # stashed complete_step args (issues 5/6)
    show_label: bool = True            # step frame (header + breadcrumb) shown
    show_output: bool = False          # surface a script's stdout as a • message
    position_label: bool = False       # step label is a position counter, not a verb
    label: str | None = None           # step display label, most-recent-wins:
                                       # the step_started seed, then overwritten by
                                       # each update_step_label push
    header_label: str | None = field(init=False, default=None)  # own header: the
                                       # creation seed, re-seeded only by a reopen restart
    lane_wait_label: str | None = None # transient footer override while parked in a
                                       # resource (test/merge) lane wait; None when
                                       # unset; highest precedence over `label` in
                                       # the live footer, never persisted
    lane_wait: bool = False            # parked in a lane semaphore wait (test or merge); live-only
    phase_label: str | None = None     # owning phase's rendered label
    command: str | None = None         # script step's shell command shown on the status line
    resumed: bool = False              # own header emitted on a genuine resume (a new
                                       # node or a reopen restart); continuations add a
                                       # StepHeaderNode instead
    route_label: str | None = None     # route reason (rule label) of the entering rule
    rotate_phrase: bool = False        # opt-in: rotate status phrase per work segment
    rotated_phrase: str | None = None  # per-segment phrase; live status line only, never persisted
    closed: bool = False
    backoff_wait_s: float = 0.0        # this step's own accumulated backoff-wait
                                       # seconds (timer honesty)
    interrupted: bool = False
    collapsible = False

    def __post_init__(self) -> None:
        self.header_label = self.label


@dataclass
class StepHeaderNode:
    """A mid-step header (a resume point or a position change); fields fixed at creation."""
    label: str
    resumed: bool
    phase_label: str | None
    model: str | None
    route_label: str | None
    collapsible = False


@dataclass
class AgentTextNode:
    payload: dict[str, Any]            # the agent_message payload
    _cache: Any = field(default=None, repr=False)  # cached RenderedMessage (F5)
    held: bool = False  # True until agent_result, step_completed, or agent_message resolves it
    # Provider-error prose, withheld until the turn's outcome resolves it.
    error_pending: bool = False
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
            from imp.cli.render_helpers import render_agent_message
            self._cache = render_agent_message(self.payload)
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
class EngineMessageNode:
    text: str
    subtype: str            # system_prompt | agent_prompt | continuation | completion
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
    provider: str | None = None
    countdown_total: float | None = None
    kind: str | None = None            # active step kind, for context-bar gating at compose time
    lane_wait: bool = False            # active step parked in a lane semaphore wait (test or merge)
    dep_wait: bool = False             # active step parked waiting on an unmerged dependency
    # Row-3 position segments, pre-split (compare=False) so the driver never re-slices `position`.
    phase: str | None = field(default=None, compare=False)     # phase head (step-blue)
    plan: str | None = field(default=None, compare=False)      # plan counter (step-blue)
    counter: str | None = field(default=None, compare=False)   # running step's label (step-blue)
    # Live-timer block-age source: block start_ts, the builder whose backoff to
    # exclude, and the backoff already accrued at start_ts.
    start_ts: str | None = field(default=None, compare=False)
    backoff_source: Any = field(default=None, compare=False)
    backoff_base: float = field(default=0.0, compare=False)


@dataclass(frozen=True)
class Carrier:
    """Who the footer's right group is about: the provider named, that provider's
    own reported auth mode, and its latest quota reading — resolved as one value,
    so a mode can never be shown against another provider's name."""
    provider: str
    auth_mode: str | None
    quota: QuotaReading | None


def carrier_provider(event: dict[str, Any]) -> str | None:
    """The provider this event states as the footer's carrier, else None: a
    step-level (non-`sub:`) agent_message naming a provider. A subagent can run a
    different provider than its step, and the group has always meant the step's."""
    if event.get("type") != "agent_message":
        return None
    agent_id = event.get("agent_id")
    if isinstance(agent_id, str) and agent_id.startswith("sub:"):
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    provider = payload.get("provider")
    return provider if isinstance(provider, str) else None


def carrier_from_events(events: Iterable[dict[str, Any]]) -> Carrier | None:
    """The carrier a quest's event log states, for a surface with no live builder:
    the provider of the last step-level agent turn, that provider's own last stated
    auth mode, and its last quota reading. None until a turn has named one."""
    # Lazy, like the builder's own quota import: narrative stays provider-free.
    from imp.providers.quota import QuotaReading

    auth_modes: dict[str, str] = {}
    quotas: dict[str, QuotaReading] = {}
    last_provider: str | None = None
    for event in events:
        etype = event.get("type")
        if etype == "agent_message":
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            # Sourced as add() sources them: the mode account-wide (a subagent's
            # turn states it for the same account), the carrier's name step-level.
            provider = payload.get("provider")
            mode = payload.get("auth_mode")
            if isinstance(provider, str) and mode is not None:
                auth_modes[provider] = mode
            step_provider = carrier_provider(event)
            if step_provider is not None:
                last_provider = step_provider
        elif etype == "quota_reading":
            payload = event["payload"]
            quotas[str(payload["provider"])] = QuotaReading.from_payload(payload)
    if last_provider is None:
        return None
    return Carrier(provider=last_provider,
                   auth_mode=auth_modes.get(last_provider),
                   quota=quotas.get(last_provider))


@dataclass
class DepWait:
    """Footer state for a go-live blocked on unmerged deps: waited ids + pinned clock start."""
    ids: list[str]
    start_ts: str | None


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
    terminal_state: str | None = None  # 'stalled'/'interrupted'/'context_exhausted'/
                                       # 'suspended' (both providers now emit it via
                                       # kind= or [bracket]); else None
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
    detail: str = ""                   # error: --full detail block; warning: lean line
    full_only: bool = False            # built into the tree, rendered only under --full
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
    """The merged per-step completion line: `✓ <step> · <N>s` with the step's round
    and severity tally, short scalar output fields and a `· → <action>` suffix
    (omitted for a plain `continue`).
    Replaces the rendered OutcomeLine at step completion (issue 3)."""
    step_name: str
    step_label: str | None = None      # step DISPLAY label for the completion line
    kind: str | None = None
    elapsed_s: float | None = None
    action: Any = None                 # dict {verb, target?} from step_completed
    extras: list[str] = field(default_factory=list)
    round: int = 1                     # the step's per-loop entry count
    counts: tuple[int, int, int] | None = None  # (critical, major, minor) tally
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
    """Terminal quest_merged event — rendered as the friendly footer in imp log only (F5)."""
    quest_id: str
    base: str
    commits: list[dict[str, str]] = field(default_factory=list)
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
    quest_id: str
    commits: list[dict[str, str]] = field(default_factory=list)
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


# ── builder ──────────────────────────────────────────────────────────────────
from collections.abc import Iterable  # noqa: E402  (kept near builder for clarity)

_RESUMES_WAIT = frozenset({
    "user_message", "agent_message", "thinking", "tool_call",
    "agent_result", "step_started", "step_completed", "backoff_ended",
})


class NarrativeBuilder:
    """Per-agent_id state machine. add(event) routes one event into the tree.

    Routing order in add() (design §5.3):
      0. update latest_context_tokens (the context-bar value) for THIS builder's
         own agent: the top-level builder (own_agent_id None) records its non-sub:
         turns, a nested builder records the sub: turns of the subagent it
         represents; a turn that belongs to a registered descendant does not move this
         builder's bar. Token and cost totals are not this builder's job — the
         accounting path in imp.cli.accounting owns them.
      1. if agent_id is a registered descendant → delegate to its builder.
      2. else dispatch by event type at the current open scope.
    """

    def __init__(
        self,
        *,
        own_agent_id: str | None = None,
        descendant_builders: dict[str, NarrativeBuilder] | None = None,
        retired_descendant_builders: dict[str, NarrativeBuilder] | None = None,
        salvage_result_owners: dict[tuple[str | None, str], NarrativeBuilder] | None = None,
        owner_builder: NarrativeBuilder | None = None,
    ) -> None:
        self._root = WorkflowNode()
        self._warnings: list[str] = []
        self._warnings_flushed: bool = False
        self._pause_outcome_shown: bool = False
        self._latest_context_tokens: int | None = None
        self._latest_context_window: int | None = None
        # Occupancy (tokens, handoff limit) of the most recent agent_message from ANY
        # agent — subagents included, unlike the gauge fields above. Feeds the
        # reincarnating line so it can report the context that actually crossed
        # the limit, which is frequently a subagent's turn (see loko19).
        self._last_agent_context: tuple[int, int] | None = None
        # The "sub:<event_id>" this builder represents (a dispatched subagent's own
        # turns); None on the top-level builder. Gates which agent_message readings move
        # THIS builder's context gauge: the top level records its own (non-sub:) turns;
        # a nested builder records the turns of the subagent it stands for.
        self._own_agent_id = own_agent_id
        self._owner_builder = owner_builder
        self._owner_fold: SubagentFold | None = None
        self._descendant_builders = (
            descendant_builders if descendant_builders is not None else {})
        self._retired_descendant_builders = (
            retired_descendant_builders if retired_descendant_builders is not None else {})
        self._salvage_result_owners = (
            salvage_result_owners if salvage_result_owners is not None else {})
        self._current_phase: PhaseNode | None = None
        # Stack of open StepNode containers. step_started pushes one;
        # step_completed pops it. Every kind (agent/script/branch) is a
        # normal step — there is no nested gate container.
        self._step_stack: list[StepNode] = []
        self._current_burst: ToolBurstFold | None = None
        self._span_start_ts: str | None = None
        self._reset_wait: ResetWait | None = None
        self._dep_wait: DepWait | None = None
        # Per-agent backoff-wait accounting (timer honesty). This builder stands
        # for exactly one agent, so it sees only that agent's agent_retry / resume
        # events. `_accum` is the sum of completed wait spans (seconds); `_start`
        # is the ISO timestamp the current in-progress wait began, or None.
        self._backoff_wait_accum: float = 0.0
        self._backoff_wait_start: str | None = None
        # Parent-working stretch start + backoff accrued at that start. Set at the three
        # parent boundaries (step_started, user_message, subagent-return); never advanced by
        # within-stretch prose/thinking/tool bursts. Distinct from _span_start_ts (resets on prose).
        self._stretch_start_ts: str | None = None
        self._stretch_backoff_base: float = 0.0
        self._sub_by_agent_id: dict[str, SubagentFold] = {}
        # Two-dict open-fold registry. By block id (NON-EMPTY block ids only) and by
        # tool_call event id (always present, unique per mint). Every writer stamps
        # id_ns="block" on a non-empty block id; the event dict backs the ordered
        # probe that closes a legacy unstamped log.
        self._open_sub_by_block: dict[str, SubagentFold] = {}
        self._open_sub_by_event: dict[str, SubagentFold] = {}
        self._closed_sub_by_block: dict[str, SubagentFold] = {}
        # True for the window between a workflow_resumed event and the first
        # re-emitted step_started, so resume re-entry can reuse nodes instead of
        # duplicating them (see Task 10B). Default False; goto loops never set it.
        self._resuming = False
        # Last unconsumed workflow_paused: attached clients never see workflow_resumed.
        self._pending_pause: Literal["solicit", "halt"] | None = None
        # Armed by an engine_message completion nudge; the next prose agent_message
        # consumes it and drops a bare WAITING protocol reply (pure plumbing).
        # Survives non-prose events.
        self._completion_nudge_pending = False
        self._opening_intent_node: UserMessageNode | None = None
        # Optimistic reply echo: reconciled by the next user_message.
        self._pending_reply_node: UserMessageNode | None = None
        # Pre-boot spinner seed (non-TUI opening echo): armed by seed_first_spinner
        # so the footer spinner shows instantly at Enter, before the daemon's first
        # step_started supersedes it. Stays None on TUI/headless paths.
        self._seed_phrase: str | None = None
        # phrase rotation: work seen this segment, consumed at each prose/user-message boundary.
        self._segment_had_work = False
        # Deferred rename: rendered after its narration, not at the quest_renamed position.
        self._pending_rename: RenameNode | None = None
        self._pending_error_prose: AgentTextNode | None = None
        # Live-footer inputs, one slot per provider so a mixed-provider run never
        # shows one carrier's figures against the other's step.
        self._auth_mode_by_provider: dict[str, str] = {}
        self._quota_by_provider: dict[str, QuotaReading] = {}
        self._last_step_provider: str | None = None

    # -- public API -----------------------------------------------------------
    @property
    def tree(self) -> NarrativeTree:
        return NarrativeTree(root=self._root)

    @property
    def latest_context_tokens(self) -> int | None:
        """Live context occupancy of the most recent agent_message turn: the provider's
        own reading when it reported one, else the turn's billing prompt sum, which
        equals occupancy on the single-request anthropic/openai push paths. NOT a
        cumulative total. None before any has arrived."""
        return self._latest_context_tokens

    @property
    def latest_context_window(self) -> int | None:
        """Raw context window of the most recent agent_message turn: SDK-reported on
        anthropic/openai paths, codex's own derated window from agent_message_usage_patch rows.
        None before any has arrived."""
        return self._latest_context_window

    def auth_mode_for(self, provider: str | None) -> str | None:
        """auth_mode of this provider's most recent agent_message; None before one
        arrives. Feeds the footer's metered-or-subscription choice."""
        return self._auth_mode_by_provider.get(provider) if provider else None

    def quota_for(self, provider: str | None) -> QuotaReading | None:
        """This provider's latest recorded quota reading, or None before one exists."""
        return self._quota_by_provider.get(provider) if provider else None

    def carrier_for(self, provider: str | None) -> Carrier | None:
        """The right group's carrier for the active step's `provider`, falling back
        to the provider of the most recent step-level agent turn when the step
        names none — a chrome step, or a drained step stack. None until one has."""
        name = provider or self._last_step_provider
        if name is None:
            return None
        return Carrier(provider=name, auth_mode=self.auth_mode_for(name),
                       quota=self.quota_for(name))

    @property
    def current_burst(self) -> ToolBurstFold | None:
        """The open (still-accumulating) tool burst, or None. Public read-only
        accessor for the live driver's finalization check and LiveStatus."""
        return self._current_burst

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
        # The step's current label rides the position row (Activity.counter): a
        # lane-wait transient overrides it in the footer, else the single most-
        # recent-wins `label` (seed, or the agent's latest update_step_label).
        top_label = (top.lane_wait_label or top.label) if top is not None else None
        top_counter = top_label
        if self._reset_wait is not None:
            if top is None:
                position = None
            elif top.position_label:
                position = _join(top.phase_label, top_label)
            else:
                position = top.phase_label
            return Activity(
                position=position,
                verb=self._reset_wait.verb,
                model=top.model if top else None,
                provider=top.provider if top else None,
                countdown_total=self._reset_wait.total_seconds,
                kind=top_kind, lane_wait=top_lane_wait,
                phase=top_phase, plan=top_plan, counter=top_counter,
                start_ts=self._backoff_wait_start,
            )
        if self._dep_wait is not None:
            if top is None:
                position = None
            elif top.position_label:
                position = _join(top.phase_label, top_label)
            else:
                position = top.phase_label
            return Activity(
                position=position,
                verb=f"waiting for {', '.join(self._dep_wait.ids)} to merge",
                model=top.model if top else None,
                provider=top.provider if top else None,
                kind=top_kind, lane_wait=top_lane_wait,
                phase=top_phase, plan=top_plan, counter=top_counter,
                start_ts=self._dep_wait.start_ts,
                dep_wait=True,
            )
        if top is not None:
            model = top.model
            provider = top.provider
            if top.position_label:
                # A position_label step's label is a counter that rides the
                # position row and the composed `position`, never the spinner verb
                # — the rotating phrase drives the spinner. A lane-wait transient
                # still overrides the verb at highest precedence.
                position = _join(top.phase_label, top_label)
                static_verb: str | None = top.lane_wait_label or top.rotated_phrase
            else:
                position = top.phase_label
                static_verb = top.lane_wait_label or top.rotated_phrase or top.label
        else:
            model = None
            provider = None
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
                                    provider=provider,
                                    countdown_total=nested.countdown_total,
                                    kind=top_kind, lane_wait=top_lane_wait,
                                    phase=top_phase, plan=top_plan, counter=top_counter,
                                    start_ts=nested.start_ts)
            n = len(running)
            if n > 1:
                earliest = min(running, key=lambda fld: fld.start_ts or "9999")
                return Activity(position=position, verb=f"running {n} subagents",
                                model=model, provider=provider,
                                kind=top_kind, lane_wait=top_lane_wait,
                                phase=top_phase, plan=top_plan, counter=top_counter,
                                start_ts=earliest.start_ts, backoff_source=earliest.nested)
            f = running[0]
            verb = f.label or f"subagent({f.subagent_name})"
            nested = f.nested.current_activity()
            detail = nested.detail if nested is not None else None
            return Activity(position=position, verb=verb, detail=detail, model=model,
                            provider=provider,
                            kind=top_kind, lane_wait=top_lane_wait,
                            phase=top_phase, plan=top_plan, counter=top_counter,
                            start_ts=f.start_ts, backoff_source=f.nested)

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
            if top.lane_wait:
                # lane_wait_start_ts is set by the lane handlers; the fallback keeps
                # a lane_wait that somehow lacks it from regressing to a 0s timer.
                p_start = top.lane_wait_start_ts or self._stretch_start_ts or top.start_ts
                p_src, p_base = None, 0.0
            elif top.run_start_ts is not None:
                p_start, p_src, p_base = top.run_start_ts, None, 0.0
            else:
                p_start = self._stretch_start_ts or top.start_ts
                p_src, p_base = self, self._stretch_backoff_base
            if top.kind in CHROME_KINDS:
                return Activity(position=position, verb=static_verb or top.command,
                                detail=detail, model=model, provider=provider,
                                kind=top_kind, lane_wait=top_lane_wait,
                                phase=top_phase, plan=top_plan, counter=top_counter,
                                start_ts=p_start, backoff_source=p_src, backoff_base=p_base)
            return Activity(position=position, verb=static_verb or "thinking",
                            detail=detail, model=model, provider=provider,
                            kind=top_kind, lane_wait=top_lane_wait,
                            phase=top_phase, plan=top_plan, counter=top_counter,
                            start_ts=p_start, backoff_source=p_src, backoff_base=p_base)
        # No step on this builder. A dispatched subagent's nested builder reaches
        # here — carry the in-flight tool tail so the PARENT can read `nested.detail`
        # and concatenate it onto the subagent's verb.
        if detail is not None:
            return Activity(detail=detail)
        if self._current_phase is not None:
            return Activity(verb="working")
        if self._seed_phrase is not None:
            return Activity(verb=self._seed_phrase)
        return None

    def seed_identity(self, quest_id: str | None, title: str) -> None:
        self._root.id = quest_id
        self._root.title = title

    def seed_opening_intent(self, text: str) -> None:
        """Optimistic echo of the opening message: seed the committed opening-intent
        node at root so frame one shows it. Used by the non-TUI
        LiveDriver.seed_opening_intent; the _h_user_message intent-subtype branch
        reconciles the seeded _opening_intent_node in place."""
        node = UserMessageNode(text=text, interactive=True)
        self._opening_intent_node = node
        self._root.children.append(node)

    def seed_first_spinner(self) -> None:
        """Arm a pre-boot spinner phrase so the live footer shows `⠋ <phrase>` the
        instant the opening message echoes, before the daemon's first step_started.
        Drawn once and held stable so the ~100ms footer redraw never re-randomizes
        the verb; superseded naturally once a step starts (the seed branch sits at
        the tail of current_activity(), after the phase/step checks)."""
        self._seed_phrase = _draw_status_phrase()

    def seed_pending_reply(self, text: str) -> None:
        """Optimistic echo for a your-turn reply; a second seed while outstanding is a no-op."""
        if self._pending_reply_node is not None:
            return
        node = UserMessageNode(text=text, interactive=True)
        self._pending_reply_node = node
        self._append(node)

    def append_committed_reply(self, text: str) -> None:
        """Append a committed interactive reply without arming _pending_reply_node."""
        self._append(UserMessageNode(text=text, interactive=True))

    def _outermost_builder(self) -> NarrativeBuilder:
        builder = self
        while builder._owner_builder is not None:
            builder = builder._owner_builder
        return builder

    def _resume_close_folds(self, agent_id: str, event: Any) -> None:
        ts = event.get("timestamp") if isinstance(event, dict) else None
        for fold in list(self._sub_by_agent_id.values()):
            if fold.opener_agent_id == agent_id and not fold.awaits_result:
                self._provisionally_close_subtree(fold, ts)

    def _remember_salvage_closed(self, fold: SubagentFold) -> None:
        if fold.block_id:
            self._closed_sub_by_block[fold.block_id] = fold
            self._salvage_result_owners[(fold.opener_agent_id, fold.block_id)] = self

    def _mark_descendant_subtree_interrupted(self, fold: SubagentFold) -> None:
        for child in list(fold.nested._sub_by_agent_id.values()):
            fold.nested._mark_descendant_subtree_interrupted(child)
        fold.running = False
        fold.terminal_state = "interrupted"

    def mark_subagents_interrupted(self) -> None:
        for fold in list(self._sub_by_agent_id.values()):
            self._mark_descendant_subtree_interrupted(fold)

    def _retire_descendant_subtree(self, fold: SubagentFold) -> None:
        child_agent_id = f"sub:{fold.event_id}"
        for child in list(fold.nested._sub_by_agent_id.values()):
            fold.nested._retire_descendant_subtree(child)
        self._sub_by_agent_id.pop(child_agent_id, None)
        self._descendant_builders.pop(child_agent_id, None)
        self._retired_descendant_builders[child_agent_id] = fold.nested

    def _provisionally_close_subtree(
        self,
        fold: SubagentFold,
        ts: str | None,
        terminal_state: str | None = None,
    ) -> None:
        fold.nested._finalize_direct_descendant_subtrees(ts, terminal_state)
        fold.nested._release_pending_error_prose()
        self._open_sub_by_block.pop(fold.block_id, None)
        self._open_sub_by_event.pop(fold.event_id, None)
        fold.running = False
        fold.end_ts = ts
        if terminal_state is not None:
            fold.terminal_state = terminal_state
        self._remember_salvage_closed(fold)
        self._retire_descendant_subtree(fold)

    def _finalize_direct_descendant_subtrees(
        self,
        ts: str | None,
        terminal_state: str | None = None,
    ) -> None:
        for fold in list(self._sub_by_agent_id.values()):
            self._provisionally_close_subtree(fold, ts, terminal_state)

    def add(self, event: Any, *, _routed_salvage: bool = False) -> None:
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

        # 0. context-bar update: latest_context_tokens tracks THIS builder's own agent.
        if etype == "agent_message":
            prov = payload.get("provider")
            auth = payload.get("auth_mode")
            # Only a stated mode is recorded: a subagent's turn carries the same
            # provider and states none, and must not erase what the step reported.
            if isinstance(prov, str) and auth is not None:
                self._auth_mode_by_provider[prov] = auth
            step_prov = carrier_provider(event)
            if step_prov is not None:
                self._last_step_provider = step_prov
            usage = payload.get("usage")
            pt = usage_prompt_tokens(usage)
            ctoks = payload.get("context_tokens")
            cwin_any = payload.get("context_window")
            is_subagent = isinstance(agent_id, str) and agent_id.startswith("sub:")
            # pt==0 is an all-zero "empty reply" (e.g. freshly-resumed
            # claude) — no gauge signal, hold last good values. pt is None
            # (usage absent) is distinct: a provider reading still updates.
            if pt != 0 and (not is_subagent or agent_id == self._own_agent_id):
                # The provider's own reading is true occupancy; the billing sum only
                # equals it on the single-request anthropic/openai push paths. With
                # neither, the slot holds rather than blanking the gauge.
                occupancy = ctoks if ctoks is not None else pt
                if occupancy is not None:
                    self._latest_context_tokens = occupancy
                if cwin_any is not None:
                    self._latest_context_window = cwin_any
            # Separate from the top-level gauge above (which drops subagents):
            # remember this turn's occupancy against the handoff limit, regardless
            # of agent, so a following reincarnating event can name the context
            # that crossed it.
            if ctoks is not None:
                from imp.providers.catalog import handoff_limit
                limit = handoff_limit(cwin_any)
                if limit is not None:
                    self._last_agent_context = (ctoks, limit)
        elif etype == "agent_message_usage_patch":
            # codex attaches occupancy AFTER the turn, on this bookkeeping row —
            # its agent_message carries usage={}, so the branch above sees nothing.
            # Same assignments, same own-agent routing rule; pre-routing so a
            # registered subagent's crossing still reaches the reincarnating line.
            from imp.providers.catalog import handoff_limit
            ctoks = payload.get("context_tokens")
            cwin = payload.get("context_window")
            limit = handoff_limit(cwin)
            if ctoks is not None and limit is not None:
                is_subagent = isinstance(agent_id, str) and agent_id.startswith("sub:")
                if not is_subagent or agent_id == self._own_agent_id:
                    self._latest_context_tokens = ctoks
                    self._latest_context_window = cwin
                self._last_agent_context = (ctoks, limit)
        elif etype == "quota_reading":
            # Account-wide, so it is retained whichever agent read it — which is why
            # this sits above the subagent delegation that would otherwise hide it.
            from imp.providers.quota import QuotaReading
            self._quota_by_provider[str(payload["provider"])] = (
                QuotaReading.from_payload(payload))
        if (not _routed_salvage and etype == "tool_result"
                and payload.get("id_ns") == "block"):
            salvage_owner = self._salvage_result_owners.pop(
                (agent_id if isinstance(agent_id, str) else None,
                 str(payload.get("id", ""))), None)
            if salvage_owner is not None:
                salvage_owner.add(event, _routed_salvage=True)
                return

        if (not _routed_salvage and isinstance(agent_id, str)
                and agent_id in self._retired_descendant_builders):
            retired = self._retired_descendant_builders[agent_id]
            fold = retired._owner_fold
            root = self._outermost_builder()
            prefix = (
                f"{fold.subagent_name} [{agent_id}]: "
                if fold is not None else f"[{agent_id}]: ")
            if etype in ("runner_warning", "warning"):
                warned = dict(payload)
                warned["message"] = prefix + str(payload.get("message", "?"))
                root._HANDLERS[etype](root, event, warned)
            elif etype == "sandbox_overlay_unavailable":
                warned = dict(payload)
                warned["reason"] = prefix + str(payload.get("reason", "?"))
                root._HANDLERS[etype](root, event, warned)
            elif etype == "error":
                node = _error_node(payload, prefix=prefix)
                if node is not None:
                    root._append(node)
            elif etype == "tool_result" and payload.get("id_ns") is not None:
                rid = str(payload.get("id", ""))
                root._append(ErrorNode(
                    message=(prefix + f"task result {rid!r} "
                             f"(id_ns={payload.get('id_ns')!r}) matched no open fold"),
                    is_warning=True))
            return

        if (not _routed_salvage and isinstance(agent_id, str)
                and agent_id != self._own_agent_id
                and agent_id in self._descendant_builders):
            descendant = self._descendant_builders[agent_id]
            descendant.add(event)
            fold = descendant._owner_fold
            if fold is not None:
                fold.event_count += 1
                if etype == "agent_retry":
                    cat = payload.get("category", "?")
                    secs = payload.get("backoff_seconds", "?")
                    hinted = bool(payload.get("hinted")) or (
                        isinstance(secs, (int, float)) and secs >= 120)
                    if hinted and isinstance(secs, (int, float)):
                        self._outermost_builder()._append(ErrorNode(
                            message=(f"[{cat}] {fold.subagent_name} waiting for limit reset"
                                     f" — retry {payload.get('attempt', '?')} in {_fmt_hm(secs)}"),
                            category=cat, is_warning=True))
            return

        had_running = bool(self.running_subagents())

        if etype in _RESUMES_WAIT:
            if self._backoff_wait_start is not None:
                span = elapsed_seconds(self._backoff_wait_start, event.get("timestamp")) or 0.0
                self._backoff_wait_accum += span
                if self._step_stack:
                    self._step_stack[-1].backoff_wait_s += span
                self._backoff_wait_start = None
            self._reset_wait = None
            self._dep_wait = None

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
                or (etype == "agent_message"
                    and not _agent_message_is_turn_end(payload)))
            matched = (
                etype == "tool_result"
                and (rid_ in self._open_sub_by_block or rid_ in self._open_sub_by_event))
            if not matched and not is_task_call and not is_parent_mid_turn:
                self._resume_close_folds(agent_id, event)

        # A bookkeeping row carries no render ordering, so it resolves nothing.
        if (self._pending_error_prose is not None
                and _resolves_error_prose(etype, payload)):
            # Only an error that carries the prose onward may drop it.
            if etype == "error" and _error_carries_prose(payload):
                self._drop_pending_error_prose()
            else:
                self._release_pending_error_prose()

        # 2. dispatch by type.
        handler = self._HANDLERS.get(etype)
        if handler is None:
            self._append(FallbackLine(event_type=etype, payload=payload))
            return
        handler(self, event, payload)

        if had_running and not self.running_subagents():
            # The parent regained the live block: its last subagent finished this
            # event (canonical close, resume-close fallback, or step-end close).
            # Start a fresh parent-working stretch here.
            self._stretch_start_ts = event.get("timestamp")
            self._stretch_backoff_base = self.backoff_wait_seconds(None)

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
        # A tag never outlives its stream: still pending at the end means released.
        self._release_pending_error_prose()
        for descendant in self._descendant_builders.values():
            descendant._release_pending_error_prose()
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

    def _release_pending_error_prose(self) -> None:
        node = self._pending_error_prose
        if node is None:
            return
        node.error_pending = False
        self._pending_error_prose = None

    def _drop_pending_error_prose(self) -> None:
        node = self._pending_error_prose
        if node is None:
            return
        self._pending_error_prose = None
        # Cleared before the search: an escaped node must not stay pending and block the cursor.
        node.error_pending = False
        children = self._container().children
        for i, c in enumerate(children):
            if c is node:
                del children[i]
                return

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


# Handlers populate this in subsequent tasks; quest_created lives here so the
# skeleton test passes.
def _h_quest_created(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # Any child-creation event carries a `kind`; only the root creation is kind-less.
    if p.get("kind") is not None:
        return
    self._root.title = p.get("title", "")
    self._root.id = p.get("id")


NarrativeBuilder._HANDLERS["quest_created"] = _h_quest_created


def _h_quest_renamed(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # A rename carries the new title. Apply it; ignore a legacy/partial event that
    # is missing new_title. The id is immutable; the breadcrumb shows the new title.
    new_title = p.get("new_title")
    if new_title is None:
        return
    self._root.title = new_title
    # Stash the rename; flushed after the narration or at the next boundary; latest title wins.
    self._pending_rename = RenameNode(name=new_title)


NarrativeBuilder._HANDLERS["quest_renamed"] = _h_quest_renamed


def _h_quest_merged(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_pending_rename()
    self._flush_burst()
    self._root.status = "completed"
    descriptor = self._root.id or ""
    # Append at the WORKFLOW ROOT (not the still-open phase): the merged footer is a
    # run-level terminal line that ends the whole transcript, after every phase.
    self._flush_warnings_summary()
    self._root.children.append(MergedNode(quest_id=descriptor,
                                          base=str(p.get("base", "")),
                                          commits=p.get("commits", [])))


NarrativeBuilder._HANDLERS["quest_merged"] = _h_quest_merged


def _h_workflow_completed(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_pending_rename()
    self._flush_burst()
    self._root.status = "completed"
    descriptor = self._root.id or ""
    # Append at the WORKFLOW ROOT: a run-level terminal line ending the whole
    # transcript, after every phase.
    self._flush_warnings_summary()
    self._root.children.append(CompletedNode(quest_id=descriptor,
                                             commits=p.get("commits", [])))


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
    # Lane-waits are live-footer-only: no scrollback node. Override the footer verb
    # with the merge-queue label (count updates in place) until lane_acquired clears it.
    text = f"waiting to merge ({p.get('ahead', 0)} ahead)"
    step = self._step_stack[-1] if self._step_stack else None
    if step is not None:
        step.lane_wait_label = text
        step.lane_wait = True
        if step.lane_wait_start_ts is None:
            step.lane_wait_start_ts = event.get("timestamp")


NarrativeBuilder._HANDLERS["lane_waiting"] = _h_lane_waiting


def _h_waiting_for_test_lane(
    self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]
) -> None:
    self._flush_burst()
    # Lane-waits are live-footer-only: no scrollback node. The step's `started`
    # already fired, so current_activity() reads its "running tests" phrase —
    # override the footer verb with "waiting to test" until test_lane_acquired clears it.
    if self._step_stack:
        self._step_stack[-1].lane_wait_label = "waiting to test"
        self._step_stack[-1].lane_wait = True
        self._step_stack[-1].lane_wait_start_ts = event.get("timestamp")


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
        top.lane_wait_label = None
        top.lane_wait = False
        ts = event.get("timestamp")
        if ts is not None:
            top.run_start_ts = ts


NarrativeBuilder._HANDLERS["test_lane_acquired"] = _h_test_lane_acquired


def fmt_ctx_tokens(n: int) -> str:
    """Compact token count for the gauge tail and the reincarnating line: '167k'
    at/above 1k, else bare."""
    return f"{round(n / 1000)}k" if n >= 1000 else str(n)


def _h_reincarnating(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_burst()
    reason = p.get("reason")
    text = f"reincarnating — {reason}" if reason else "reincarnating"
    # Name the occupancy that crossed the handoff limit (tokens/limit), so the
    # line is self-explaining and the denominator is unambiguous — no re-deriving
    # it from cumulative token counters, which read far larger than live context.
    ctx = self._last_agent_context
    if ctx is not None:
        text += f" ({fmt_ctx_tokens(ctx[0])}/{fmt_ctx_tokens(ctx[1])})"
    self._append(LiteralTextNode(text=text, color_role="lifecycle"))


NarrativeBuilder._HANDLERS["reincarnating"] = _h_reincarnating


def _h_lane_event(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_burst()


def _h_lane_acquired(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_burst()
    if self._step_stack:
        top = self._step_stack[-1]
        top.lane_wait_label = None
        top.lane_wait = False


NarrativeBuilder._HANDLERS["lane_acquired"] = _h_lane_acquired
NarrativeBuilder._HANDLERS["lane_released"] = _h_lane_event
NarrativeBuilder._HANDLERS["lane_evicted"] = _h_lane_event
NarrativeBuilder._HANDLERS["lane_restored"] = _h_lane_event


def _h_dependency_waiting(
    self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]
) -> None:
    self._flush_burst()
    ids = list(p.get("depends_on", []))
    start = self._dep_wait.start_ts if self._dep_wait is not None else event.get("timestamp")
    self._dep_wait = DepWait(ids=ids, start_ts=start)


NarrativeBuilder._HANDLERS["dependency_waiting"] = _h_dependency_waiting


def _h_dependency_ready(
    self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]
) -> None:
    self._flush_burst()
    self._dep_wait = None


NarrativeBuilder._HANDLERS["dependency_ready"] = _h_dependency_ready


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


def _append_resume_header(step: StepNode, route_label: str | None) -> None:
    if step.show_label:
        step.children.append(StepHeaderNode(
            label=step.label or step.name, resumed=True, phase_label=step.phase_label,
            model=step.model, route_label=route_label))


def _h_step_started(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_pending_rename()
    self._flush_burst()
    # Consumed HERE, not at the _resuming capture below: the internal-retry branch
    # returns early and must still see it.
    pause, self._pending_pause = self._pending_pause, None
    # A your-turn reply is never a resume; a crash recovery journals no pause at all.
    genuine_signal = pause == "halt" or (self._resuming and pause is None)
    self._span_start_ts = event.get("timestamp")
    self._stretch_start_ts = event.get("timestamp")
    self._stretch_backoff_base = self.backoff_wait_seconds(None)
    self._segment_had_work = False
    # New step = fresh agent conversation; prior step's window must not scale this bar.
    self._latest_context_tokens = None
    self._latest_context_window = None
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
                    top.label = p.get("label")
                    top.lane_wait_label = None
                elif p.get("route_label"):
                    top.label = p.get("label")
                if genuine_signal:
                    _append_resume_header(top, p.get("route_label"))
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
            # node for appending.
            existing.outcome = None
            existing.closed = False
            existing.interrupted = False
            genuine = bool(p.get("resumed", False)) and genuine_signal
            if p.get("restarted"):
                # Genuine re-run from scratch: the fresh run supersedes the
                # interrupted partial, so revert the label to the re-emitted seed,
                # clear the lane transient, wipe the partial, restamp the clock,
                # and reset the re-run-only fields. Interactivity is re-sourced
                # from the event stream (a re-emitted awaiting_user_input re-arms it).
                existing.label = p.get("label")
                existing.lane_wait_label = None
                existing.lane_wait = False
                existing.lane_wait_start_ts = None
                existing.children.clear()
                existing.kind = kind
                existing.model = p.get("model")
                existing.provider = p.get("provider")
                existing.interactive = False
                existing.start_ts = event.get("timestamp")
                existing.run_start_ts = None
                existing.agent_result = None
                existing.command = p.get("command")
                existing.show_label = bool(p.get("show_label", True))
                existing.show_output = bool(p.get("show_output", False))
                existing.phase_label = p.get("phase_label")
                existing.route_label = p.get("route_label")
                existing.rotate_phrase = bool(p.get("rotate_phrase", False))
                existing.position_label = bool(p.get("position_label", False))
                existing.rotated_phrase = (
                    _draw_status_phrase() if existing.rotate_phrase else None)
                existing.header_label = existing.label
                existing.resumed = genuine
            elif p.get("route_label"):
                # A continuation that re-entered via a labeled edge shows that
                # reason; a plain continuation preserves the pushed label.
                existing.label = p.get("label")
            if genuine and not p.get("restarted"):
                _append_resume_header(existing, p.get("route_label"))
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
                    round=p.get("round", 1),
                    show_label=bool(p.get("show_label", True)),
                    show_output=bool(p.get("show_output", False)),
                    position_label=bool(p.get("position_label", False)),
                    label=p.get("label"),
                    phase_label=p.get("phase_label"),
                    command=p.get("command"),
                    resumed=bool(p.get("resumed", False)) and genuine_signal,
                    route_label=p.get("route_label"),
                    rotate_phrase=bool(p.get("rotate_phrase", False)))
    self._append(node)
    self._step_stack.append(node)
    if node.rotate_phrase:
        node.rotated_phrase = _draw_status_phrase()


def _h_step_completed(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_pending_rename()
    self._flush_burst()
    self._finalize_direct_descendant_subtrees(event.get("timestamp"))
    action = p.get("action")          # dict {verb, target?}; var_mutations is a sibling
    node = self._pop_step(p.get("step"))
    if node is not None:
        # Un-hold any trailing held narration — agent_result won't fire for
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
            extras=_breadcrumb_extras(node.agent_result),
            round=node.round,
            counts=severity_counts(node.agent_result),
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
    # Above the `if reason:` gate and its early returns: those suppress a redundant
    # rendered pause line, they do not mean the run did not pause.
    self._pending_pause = "solicit" if p.get("reason") == "awaiting_user" else "halt"
    self._flush_pending_rename()
    self._flush_burst()
    self._root.status = "paused"
    self._finalize_direct_descendant_subtrees(event.get("timestamp"), "interrupted")
    reason = p.get("interrupt_reason") or p.get("reason")
    if reason:
        # A genuine your-turn yield is a normal solicit, not an interrupt: the
        # agent's prose and the eventual reply already carry it, so the pause
        # line is redundant. Finalize folds and mark paused above, but append no
        # PauseNode. (Shared render path — covers both imp log and the live view.)
        if reason == "awaiting_user":
            return
        if reason in _HUMAN_INTERRUPT_REASONS:
            # No PauseNode; unlike awaiting_user, keep the flag so the digest flushes.
            self._pause_outcome_shown = True
            return
        # Anchor the pause in the innermost open scope so it renders where it fired.
        self._append(PauseNode(reason=reason))
        self._pause_outcome_shown = True


def _h_workflow_resumed(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_burst()
    self._root.status = "running"
    self._finalize_direct_descendant_subtrees(event.get("timestamp"), "interrupted")
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
    # blank-line spacing in render_text). The event's prompt payload is carried
    # onto the pending reply as its question line (M3); render nothing here — the
    # reply node renders the question above its caret.
    if self._step_stack:
        self._step_stack[-1].interactive = True
    # Settle an open tool burst so the live _walk does not block on it: a burst
    # is not closed by its own tool_result (only by the next prose/flush), so a
    # replayed tail that ran a tool then solicited the human without closing
    # prose leaves current_burst set across the replay_complete boundary. That
    # keeps _is_final False for the burst, the walk stops there, and the first
    # post-boundary reply node is never reached — its echo is dropped until the
    # daemon's user_message flushes. awaiting_user_input is always a real solicit
    # boundary, so flush unconditionally. Mirrors the un-hold loop below and the
    # flush in _h_agent_message / _h_user_message.
    self._flush_burst()
    self.mark_subagents_interrupted()
    # Un-hold any held narration in the current container: the prose carried by
    # the agent_message context the human must read to answer, and the agent is now blocked
    # settled, NOT a trailing narration that could still fold into a step
    # conclusion. Without this the live _walk blocks on the held node
    # (_is_final is False for a held AgentTextNode) and the message surfaces only
    # after the user replies (the next agent_message un-holds it). Mirrors the
    # un-hold-all loop in _h_agent_message.
    for c in self._container().children:
        if isinstance(c, AgentTextNode) and c.held:
            c.held = False
    return None


NarrativeBuilder._HANDLERS["awaiting_user_input"] = _h_awaiting_user_input


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



def _resolves_error_prose(etype: str, payload: dict[str, Any]) -> bool:
    """True for an event that carries render ordering; a bookkeeping row does not."""
    if etype in ("query_result", "agent_message_usage_patch"):
        return False
    if etype == "error":
        return not payload.get("audit")
    return etype in NarrativeBuilder._HANDLERS


def _error_carries_prose(payload: dict[str, Any]) -> bool:
    """True when the error payload holds the withheld prose; a text-less one has no carrier."""
    detail = payload.get("detail")
    return isinstance(detail, dict) and bool(detail.get("last_agent_text"))


def _last_narration_index(children: list[Any]) -> int | None:
    """Trailing AgentTextNode index, scanning past any trailing ToolBurstFold nodes."""
    i = len(children) - 1
    while i >= 0 and isinstance(children[i], ToolBurstFold):
        i -= 1
    if i >= 0 and isinstance(children[i], AgentTextNode):
        return i
    return None


TALLY_FIELDS = ("critical", "major", "minor")


def severity_counts(args: Any) -> tuple[int, int, int] | None:
    """The reviewer's (critical, major, minor) tally, or None when the step's
    output carries no counts. All three or nothing — a partial tally has no
    honest rendering."""
    if not isinstance(args, dict):
        return None
    critical, major, minor = (args.get(k) for k in TALLY_FIELDS)
    if isinstance(critical, int) and isinstance(major, int) and isinstance(minor, int):
        return critical, major, minor
    return None


def _breadcrumb_extras(args: Any) -> list[str]:
    """Short scalar output fields (`status`, `verdict`, `*_path`, numbers, bools,
    short strings) folded into the breadcrumb as `key=value`. Prose fields, the
    severity counts (which render as a tally) and non-scalar values (dicts/lists)
    are skipped."""
    if not isinstance(args, dict):
        return []
    out: list[str] = []
    for k, v in args.items():
        if k in TALLY_FIELDS or is_prose_field(k, v):
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


def _union_seconds(segments: list[tuple[float, float]]) -> float:
    """Total length of the union of half-open segments: overlaps counted once."""
    total = 0.0
    mark = float("-inf")
    for lo, hi in sorted(segments):
        lo = max(lo, mark)
        if hi > lo:
            total += hi - lo
        mark = max(mark, hi)
    return total


@dataclass(frozen=True)
class Exclusions:
    """Wall-clock intervals of a run that are not work: human waits and pauses."""

    spans: tuple[tuple[str, str], ...] = ()

    def overlap(self, start_ts: str | None, end_ts: str | None) -> float:
        """Union seconds falling inside [start_ts, end_ts]; 0.0 for an open span."""
        width = elapsed_seconds(start_ts, end_ts)
        if width is None or width <= 0:
            return 0.0
        clipped: list[tuple[float, float]] = []
        for open_ts, close_ts in self.spans:
            lo = elapsed_seconds(start_ts, open_ts)
            hi = elapsed_seconds(start_ts, close_ts)
            if lo is None or hi is None:
                continue
            lo, hi = max(0.0, lo), min(width, hi)
            if hi > lo:
                clipped.append((lo, hi))
        return _union_seconds(clipped)

    @property
    def total(self) -> float:
        """Union seconds over the whole run."""
        if not self.spans:
            return 0.0
        ref = self.spans[0][0]  # spans aren't in start order, so offsets are deliberately signed
        segments: list[tuple[float, float]] = []
        for open_ts, close_ts in self.spans:
            lo = elapsed_seconds(ref, open_ts)
            hi = elapsed_seconds(ref, close_ts)
            if lo is not None and hi is not None and hi > lo:
                segments.append((lo, hi))
        return _union_seconds(segments)


class ExclusionTracker:
    """Human waits (`awaiting_user_input` → the reply) and pauses (`workflow_paused`
    → `workflow_resumed`), each also closed by the step boundary that ends it."""

    def __init__(self) -> None:
        self._spans: list[tuple[str, str]] = []
        self._wait_start: str | None = None
        self._pause_start: str | None = None

    @property
    def exclusions(self) -> Exclusions:
        """The spans closed so far; a still-open wait or pause is not included."""
        return Exclusions(tuple(self._spans))

    def feed(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        ts = event.get("timestamp")
        if not isinstance(ts, str):
            return
        if etype == "awaiting_user_input":
            if self._wait_start is None:  # a re-armed gate keeps the first ask
                self._wait_start = ts
        elif etype == "workflow_paused":
            if self._pause_start is None:
                self._pause_start = ts
        elif etype == "user_message":
            self._close_wait(ts)
        elif etype == "workflow_resumed":
            self._close_pause(ts)
        elif etype in ("step_started", "step_completed"):
            self._close_wait(ts)
            self._close_pause(ts)

    def _close_wait(self, ts: str) -> None:
        if self._wait_start is not None:
            self._spans.append((self._wait_start, ts))
            self._wait_start = None

    def _close_pause(self, ts: str) -> None:
        if self._pause_start is not None:
            self._spans.append((self._pause_start, ts))
            self._pause_start = None


def activity_elapsed(activity: Activity, now_iso: str) -> float:
    """Block-age: now - start_ts, minus in-block backoff; 0.0 if missing, never negative."""
    span = elapsed_seconds(activity.start_ts, now_iso)
    if span is None:
        return 0.0
    if activity.backoff_source is not None:
        span -= max(0.0, activity.backoff_source.backoff_wait_seconds(now_iso)
                    - activity.backoff_base)
    return max(0.0, span)


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


def _h_engine_message(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    subtype = str(p.get("subtype") or "")
    if subtype == "completion":
        # Preserve old completion_nudge behavior exactly: arm the WAITING-drop and
        # render nothing structural. Do NOT flush — a turn-end nudge must not split
        # an open tool burst (byte-identity, AC #1).
        self._completion_nudge_pending = True
    else:
        self._flush_burst()
    if subtype == "agent_prompt":
        # Replicate the old human=False user_message seed: reseed the burst/stretch
        # span clock from the body ts for agent_prompt ONLY, so lean imp log stays
        # byte-identical (system_prompt/continuation/completion never seeded span).
        _rotate_status_phrase(self)
        self._span_start_ts = event.get("timestamp")
        self._stretch_start_ts = event.get("timestamp")
        self._stretch_backoff_base = self.backoff_wait_seconds(None)
    self._append(EngineMessageNode(text=_user_message_text(p), subtype=subtype))


def _h_user_message(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_burst()
    _rotate_status_phrase(self)
    self._span_start_ts = event.get("timestamp")
    self._stretch_start_ts = event.get("timestamp")
    self._stretch_backoff_base = self.backoff_wait_seconds(None)
    subtype = p.get("subtype")
    text = _user_message_text(p)
    if subtype == "reply" and not text.strip():
        block = _sole_success_tool_result(p)
        if block is not None:
            # Live-safe resume reply: the sole block is a success tool_result
            # carrying the human's reply (anthropic/openai/claude resume shape).
            text = _summarize_result(block.get("content"))
    if event.get("_replay"):
        if subtype == "intent" and self._opening_intent_node is not None:
            # Replayed opening intent with an existing optimistic echo: adopt text in place.
            self._opening_intent_node.text = text
            return
        # A replayed user_message never consumes an optimistic echo — only the live turn does.
        node = UserMessageNode(text=text, interactive=True)
        if subtype == "intent":
            self._opening_intent_node = node
        self._append(node)
        return
    if subtype == "intent" and self._opening_intent_node is not None:
        # Already seeded optimistically (non-TUI live path): adopt the authoritative
        # text in place; never append a duplicate.
        self._opening_intent_node.text = text
        return
    pending, self._pending_reply_node = self._pending_reply_node, None
    if pending is not None:
        # Reconcile the optimistic echo: adopt authoritative text in place
        # (one-to-one awaiting-reply match).
        pending.text = text
        pending.interactive = True
        return
    node = UserMessageNode(text=text, interactive=True)
    if subtype == "intent":
        self._opening_intent_node = node
    self._append(node)


def _h_agent_message(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # Token accumulation already happened in add(); here we only build the node.
    # §8: an empty-text agent_message is a tool-use-turn carrier (a lone tool_use
    # block). It must NOT flush an open burst — otherwise consecutive tool calls
    # each start a fresh burst and the status counter sticks at 1. Gate on the
    # shared prose predicate.
    if not agent_message_has_prose(p):
        return
    # The handoff note reaches the reborn agent inlined in its seed; it is private.
    if p.get("handoff"):
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
    if p.get("provider_error"):
        node.error_pending = True
        self._pending_error_prose = node
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


def _h_agent_result(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # agent_result (= complete_step args) fires BEFORE step_completed. Stash the
    # args on the open StepNode: scalars fold into the breadcrumb, the full args
    # surface only under `--full`. A tool call's arguments are plumbing, never a
    # prose block — the agent's narration is the single rendered copy (E1).
    if not self._step_stack:
        return
    self._flush_burst()
    step = self._step_stack[-1]
    step.agent_result = p
    # Un-hold the agent's final narration so the live driver emits it now: the
    # agent has yielded, so its trailing prose is settled, not a held tail.
    children = self._container().children
    narration_idx = _last_narration_index(children)
    if narration_idx is not None:
        children[narration_idx].held = False


def _h_query_result(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    return  # no visible node


def _h_agent_message_usage_patch(
        self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # Gauge fields already folded in add(); the row carries no render ordering.
    return


NarrativeBuilder._HANDLERS["user_message"] = _h_user_message
NarrativeBuilder._HANDLERS["engine_message"] = _h_engine_message
NarrativeBuilder._HANDLERS["agent_message"] = _h_agent_message
NarrativeBuilder._HANDLERS["agent_message_usage_patch"] = _h_agent_message_usage_patch
NarrativeBuilder._HANDLERS["thinking"] = _h_thinking
NarrativeBuilder._HANDLERS["step_context"] = _h_step_context
NarrativeBuilder._HANDLERS["agent_result"] = _h_agent_result
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
    fold.nested._release_pending_error_prose()
    fold.running = False
    fold.end_ts = end_ts
    fold.is_error = bool(p.get("error", False))
    fold.outcome = _summarize_result(p.get("content"))
    # Terminal state for Part D's glyph. Prefer an explicit payload kind (the
    # claude degraded-pause path sets kind="interrupted"); else detect the
    # bracketed marker an is_error failure message embeds ("... [stalled]" /
    # "... [interrupted]" — the codex degraded-pause path appends the latter).
    # The brackets keep the match precise (low false-positive).
    kind = p.get("kind")
    if kind not in ("stalled", "interrupted", "context_exhausted", "suspended"):
        # The bracketed marker only originates from the failure path; a
        # SUCCESSFUL subagent's content is agent-controlled JSON that may
        # legitimately contain "[stalled]". Gate the scan on is_error so such
        # text can't false-positive into a terminal state.
        text = fold.outcome if fold.is_error else ""
        _kinds = ("stalled", "interrupted", "context_exhausted", "suspended")
        kind = next((s for s in _kinds if f"[{s}]" in (text or "")), None)
    if kind in ("stalled", "interrupted", "context_exhausted", "suspended"):
        fold.terminal_state = kind


def _take_subagent_fold(
    self: NarrativeBuilder, rid: str, id_ns: Any,
) -> tuple[SubagentFold | None, str]:
    """Probe the id_ns namespace for rid; remove from both dicts. Returns (fold, status)."""
    if id_ns == "block":
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
    """Probe the SALVAGE-closed registry for rid; remove it so a late canonical
    result is applied at most once. Stamped ("block") results only — an unstamped
    legacy result never reaches this path (it has its own probe)."""
    if id_ns != "block":
        return None
    return self._closed_sub_by_block.pop(rid, None)


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
        fold.nested._finalize_direct_descendant_subtrees(ts)
        _close_subagent_fold(fold, p, ts)
        self._retire_descendant_subtree(fold)
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
        message = f"task result {rid!r} (id_ns={p.get('id_ns')!r}) matched no open fold"
        target = self
        if self._owner_builder is not None:
            owner = self._owner_fold
            assert owner is not None and self._own_agent_id is not None
            message = f"{owner.subagent_name} [{self._own_agent_id}]: {message}"
            target = self._outermost_builder()
        target._append(ErrorNode(message=message, is_warning=True))
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


def _latest_header_label(step: StepNode) -> str:
    for c in reversed(step.children):
        if isinstance(c, StepHeaderNode):
            return c.label
    return step.header_label or step.name


def _h_step_label_updated(
    self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]
) -> None:
    # Live status-bar label for the current step. Do NOT flush the open burst:
    # this fires between the update_step_label tool_call and its tool_result.
    if not self._step_stack:
        return
    top = self._step_stack[-1]
    top.label = p.get("label")
    if top.position_label and top.show_label:
        label = top.label or top.name
        if label != _latest_header_label(top):
            self._append(StepHeaderNode(label=label, resumed=False,
                                        phase_label=top.phase_label, model=top.model,
                                        route_label=None))


NarrativeBuilder._HANDLERS["step_label_updated"] = _h_step_label_updated


def _detail_text(d: object) -> str:
    if isinstance(d, str):
        return d
    if isinstance(d, dict):
        if d.get("full_summary"):
            return str(d["full_summary"])
        if d.get("stderr_tail"):
            tail = str(d["stderr_tail"])
            exit_code = d.get("exit_code")
            return f"exit code {exit_code}\n{tail}" if isinstance(exit_code, int) else tail
        if d.get("last_agent_text"):
            return str(d["last_agent_text"])
    return ""


def _error_node(p: dict[str, Any], prefix: str = "") -> ErrorNode | None:
    if p.get("audit"):
        return None
    if p.get("category") == "config" and p.get("kind") == "interrupted":
        return None
    detail = _detail_text(p.get("detail"))
    if p.get("category") == "stalled":
        from imp.agents.timeouts import AGENT_INACTIVITY_TIMEOUT_S
        return ErrorNode(
            message=(prefix + f"no response for {AGENT_INACTIVITY_TIMEOUT_S:.0f}s"
                     " — resuming; work already saved is preserved"),
            category="stalled", is_warning=True, detail=detail)
    from imp.engine.error_policy import is_retry_with_backoff
    if is_retry_with_backoff(str(p.get("category") or "")):
        return None
    return ErrorNode(
        message=prefix + str(p.get("message", "?")), category=p.get("category"),
        is_warning=False, detail=detail)


def _h_error(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    if p.get("audit"):
        return
    self._flush_burst()
    if p.get("category") == "config" and p.get("kind") == "interrupted":
        if self._step_stack:
            self._step_stack[-1].interrupted = True
        return
    node = _error_node(p)
    if node is not None:
        self._append(node)


def _h_runner_warning(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # Non-terminal: do NOT flush the burst (a warning can be emitted mid-burst
    # for a dropped block; flushing would split an otherwise-contiguous run).
    message = str(p.get("message", "?"))
    self._warnings.append(message)
    self._append(ErrorNode(message=message, category=p.get("category"), is_warning=True,
                           detail=_detail_text(p.get("detail"))))


def _h_warning(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # Non-terminal: does NOT flush the burst (same invariant as _h_runner_warning).
    message = str(p.get("message", "?"))
    self._warnings.append(message)
    self._append(ErrorNode(message=message, category=p.get("category"), is_warning=True,
                           detail=_detail_text(p.get("detail"))))


def _h_sandbox_overlay_unavailable(
    self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]
) -> None:
    # Non-terminal (same invariant as _h_warning): surface + tally, do not flush the burst.
    message = str(p.get("reason", "?"))
    self._warnings.append(message)
    self._append(ErrorNode(message=message, category=p.get("category"), is_warning=True,
                           detail=_detail_text(p.get("detail"))))


def _fmt_hm(seconds: float) -> str:
    """`2d 4h` / `2h 13m` / `45m` compact duration; the day form takes over at 48
    hours, where `52h 0m` stops reading as a duration. Every caller can wait days."""
    total_minutes = int(seconds // 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours >= 48:
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h"
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


def _retry_message(message: Any) -> str:
    """The breadcrumb suffix: cut to 159 plus `…` so the message is exactly 160."""
    if not message:
        return ""
    text = str(message)
    if len(text) > 160:
        text = text[:159] + "…"
    return f" — {text}"


def _h_agent_retry(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # Non-terminal: a transient error was retried with backoff. Render inline so
    # a self-healed network/rate_limit blip doesn't look like a failure.
    cat = p.get("category", "?")
    # A step-level sticky retry (unknown/terminated ridden out) logs its streamed
    # provider error first; that hard red line is now misleading because the loop
    # rode the transient out. Drop the immediately-preceding same-category
    # non-warning ErrorNode so a sticky retry renders like every other backoff
    # retry. No-op for network/auth/rate_limit — _h_error already suppressed those.
    container = self._container()
    if (container.children
            and isinstance(container.children[-1], ErrorNode)
            and not container.children[-1].is_warning
            and container.children[-1].category == cat):
        del container.children[-1]
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
    if p.get("kind") == "token_rotated":
        # The provider's 401 prose blames auth; the real cause is a peer session.
        self._append(ErrorNode(
            message=(f"[{cat}] retry {attempt} ({secs}s) — another session rotated"
                     " the OAuth token; retrying with the new one"),
            category=cat, is_warning=True,
            full_only=backoff and isinstance(attempt, int) and attempt == 1))
        return
    # Routine when it heals on retry 1 — the status line already narrates the wait.
    self._append(ErrorNode(
        message=f"[{cat}] retry {attempt} ({secs}s){_retry_message(p.get('message'))}",
        category=cat, is_warning=True,
        full_only=backoff and isinstance(attempt, int) and attempt == 1))


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
    detail = truncate_detail(detail)
    self._append(ErrorNode(
        message=f"{step} blocked{suffix}", category="script",
        is_warning=False, detail=detail))


def _h_max_tokens_truncated(
    self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]
) -> None:
    # Telemetry-only; the sibling runner_warning is the visible signal (no breadcrumb).
    return


def _h_backoff_ended(
    self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]
) -> None:
    # Wait-state only (cleared in add() via _RESUMES_WAIT); agent_retry left the breadcrumb.
    return


NarrativeBuilder._HANDLERS["error"] = _h_error
NarrativeBuilder._HANDLERS["runner_warning"] = _h_runner_warning
NarrativeBuilder._HANDLERS["warning"] = _h_warning
NarrativeBuilder._HANDLERS["sandbox_overlay_unavailable"] = _h_sandbox_overlay_unavailable
NarrativeBuilder._HANDLERS["agent_retry"] = _h_agent_retry
NarrativeBuilder._HANDLERS["output_validation_nudge"] = _h_output_validation_nudge
NarrativeBuilder._HANDLERS["script_blocked"] = _h_script_blocked
NarrativeBuilder._HANDLERS["max_tokens_truncated"] = _h_max_tokens_truncated
NarrativeBuilder._HANDLERS["backoff_ended"] = _h_backoff_ended


def _h_script_output(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_burst()
    self._append(LiteralTextNode(text=truncate_detail(str(p.get("stdout", ""))),
                                 color_role=None, verbatim=False))


NarrativeBuilder._HANDLERS["script_output"] = _h_script_output


def _h_tiers_resolved(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    # Fires once at run start with agent_id=None, BEFORE any step_started, so the
    # step stack is empty and _append lands this at the workflow root.
    # Payload: {"tiers": {tier: {provider, model, ...}}, "steps": {step: {...}}}
    tiers = p.get("tiers") or {}
    rows = [
        {"tier": tier, "provider": d.get("provider"), "model": d.get("model"),
         "differ_tier": d.get("differ_tier"), "differ_from": d.get("differ_from")}
        for tier, d in tiers.items()
    ]
    # Also add step overrides (steps with differ_from resolution)
    steps = p.get("steps") or {}
    for step_name, d in steps.items():
        rows.append({
            "tier": step_name, "provider": d.get("provider"), "model": d.get("model"),
            "differ_tier": d.get("differ_tier"), "differ_from": d.get("differ_from"),
        })
    self._append(TiersResolvedNode(rows=rows))


NarrativeBuilder._HANDLERS["tiers_resolved"] = _h_tiers_resolved


def _open_subagent_fold(self: NarrativeBuilder, event: dict[str, Any], p: dict[str, Any]) -> None:
    self._flush_burst()
    event_id = str(event.get("id", ""))
    block_id = str(p.get("id", ""))
    agent_id = event.get("agent_id")
    inp = p.get("input") or {}
    child_agent_id = f"sub:{event_id}"
    nested = NarrativeBuilder(
        own_agent_id=child_agent_id,
        descendant_builders=self._descendant_builders,
        retired_descendant_builders=self._retired_descendant_builders,
        salvage_result_owners=self._salvage_result_owners,
        owner_builder=self,
    )
    fold = SubagentFold(
        subagent_name=str(inp.get("subagent_name", "?")),
        event_id=event_id, block_id=block_id,
        nested=nested,
        opener_agent_id=agent_id if isinstance(agent_id, str) else None,
        start_ts=event.get("timestamp"),
        awaits_result=p.get("id_ns") == "block",
        label=p.get("label"),
        task_description=str(inp["task_description"]) if inp.get("task_description") else None)
    nested._owner_fold = fold
    self._append(fold)
    self._sub_by_agent_id[child_agent_id] = fold
    self._descendant_builders[child_agent_id] = nested
    self._retired_descendant_builders.pop(child_agent_id, None)
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
        return {"t": t, "quest_id": node.quest_id, "base": node.base,
                "commits": node.commits}
    if isinstance(node, CompletedNode):
        return {"t": t, "quest_id": node.quest_id, "commits": node.commits}
    return {"t": t}

# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Per-quest persistence: state.json (cursor) + events.jsonl (audit log) + locks.

Tasks 10/11/12 build this file incrementally:
  Task 10 — State
  Task 11 — EventLog
  Task 12 — ProjectLock + LockHeld
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from imp.agents.runner import Message
    from imp.providers.config import ProviderModel
    from imp.providers.quota import QuotaReading

import filelock

from imp.engine import readfault
from imp.engine.errors import StateDecodeError
from imp.engine.pagecache import drop_page_cache
from imp.engine.types import ResumeCause, ResumeTarget

Status = Literal[
    "new", "paused", "merge_blocked", "completed", "archived", "abandoned", "error"
]

# Durable gate marker written to state.json. `waiting` = parked at a human input
# gate, either live or persisted through a question pause; None = working / clean
# at rest. A not-live failure halt is the stored status `error`, not a halt-condition.
HaltCondition = Literal["waiting"]

# workflow_paused reasons that are NOT failures: a bare-turn-end yield, a lane
# turn-wait, or an interrupt. Every other reason (internal_error, retry_ceiling,
# reauth_needed, max_loops, unknown_step, provider error categories, …) is a
# failure halt. Defaulting the non-clean case keeps the mapping total with no
# error-reason list to maintain.
_CLEAN_PAUSE_REASONS = frozenset({"awaiting_user", "awaiting_turn", "interrupt"})


def classify_pause_halt(
        reason: str, interrupt_reason: str | None) -> Literal["error"] | None:
    """Map a workflow_paused (reason, interrupt_reason) to the durable
    halt-condition. A human interrupt (interrupt_reason set) or a clean
    yield/turn-wait reads clean (None); every other reason is a failure (error)."""
    if interrupt_reason is not None:
        return None
    if reason in _CLEAN_PAUSE_REASONS:
        return None
    return "error"


# Pre-release coercion of legacy persisted values: `running` was the old birth
# status (now `new`); `errored` was a derived crash label that no longer persists
# (a crash is resumable → `paused`).
_LEGACY_STATUS = {"running": "new", "errored": "paused"}


def _read_state_bytes(path: Path) -> bytes:
    """Single seam for every state.json read (initial + retries) so the
    transient-fault recovery path is exercisable in tests."""
    return path.read_bytes()


# Why a parse failure here is a read fault rather than corruption: imp.engine.readfault.
def _recover_or_raise(
        state_path: Path, raw: bytes,
        err: json.JSONDecodeError | UnicodeDecodeError) -> dict[str, Any]:
    """Re-read cold up to readfault.READ_RETRIES times, else record and raise."""
    for attempt in range(1, readfault.READ_RETRIES + 1):
        time.sleep(0.002 * attempt)       # let the transient settle; rare path
        drop_page_cache(state_path)
        try:
            retry_raw = _read_state_bytes(state_path)
            data: dict[str, Any] = json.loads(retry_raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            continue
        readfault.record("state", state_path, attempt=attempt,
                         reason=str(err), bad=raw, good=retry_raw)
        return data
    evidence = readfault.record("state", state_path, attempt=None,
                                reason=str(err), bad=raw, good=None)
    raise StateDecodeError(
        live=state_path, evidence=evidence, reason=str(err)) from err


@dataclass
class CurrentStep:
    transcript_dir: Path

    def to_dict(self) -> dict[str, Any]:
        return {"transcript_dir": str(self.transcript_dir)}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CurrentStep:
        return cls(transcript_dir=Path(raw["transcript_dir"]))


@dataclass(frozen=True)
class LaneRestore:
    """Cursor abandoned by a pause-time lane eviction, plus its safety fingerprint."""

    step: str
    base_head: str
    quest_head: str

    def to_dict(self) -> dict[str, Any]:
        return {"step": self.step, "base_head": self.base_head,
                "quest_head": self.quest_head}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LaneRestore:
        return cls(step=raw["step"], base_head=raw["base_head"],
                   quest_head=raw["quest_head"])


@dataclass
class State:
    id: str
    step: str | None
    step_attempt: int
    status: Status
    vars: dict[str, Any] = field(default_factory=dict)
    loop_counts: dict[str, int] = field(default_factory=dict)
    outcome_history: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    current_step: CurrentStep | None = None
    retry_feedback: dict[str, Any] | None = None
    resume_pending: str | None = None
    # Armed and cleared with the resume triple by the two writers below.
    resume_target: ResumeTarget | None = None
    # Resume-continue disposition for resume_pending: True ⇒ native resume + a
    # continuation nudge, False ⇒ resume without a nudge. Meaningful only while
    # resume_pending is set; armed and cleared with it via the two writers below,
    # so the pair can never tear (every read already guards on resume_pending).
    resume_should_continue: bool = False
    # Transient: never persisted, and reset by every arming and clear.
    resume_cause: ResumeCause = "human"
    resolved_tiers: dict[str, ProviderModel] | None = None
    resolved_steps: dict[str, ProviderModel] | None = None
    memory: dict[str, str] | None = None
    merge_commit: str | None = None
    step_label: str | None = None
    phase_label: str | None = None
    held_lane: str | None = None          # lane key (base branch) this quest holds
    held_acquire_step: str | None = None  # the acquiring step to rewind to on eviction
    # Consume-once: last pause kept the held merge lane; entry guard honors it once.
    lane_retained: bool = False
    # Recorded by a pause-time eviction; consumed once at re-acquisition.
    lane_restore: LaneRestore | None = None
    unsandboxed: bool = False  # step-scoped /sandbox off override; durable truth
    # here (restored into the coordinator on resume, cleared at step advance).
    route_label: str | None = None  # rule label threaded to the next step_started (consume-on-read)
    started_at: str | None = None  # durable: ISO-8601 UTC instant work began; None until first run
    # Durable gate condition (state.json). Written eagerly at a human gate
    # (waiting), kept through a question pause, cleared by every other pause,
    # when work resumes, and on normal step advance.
    halt_condition: HaltCondition | None = None
    has_started: bool = False  # durable: True once >=1 step has begun executing
    completion_pending: bool = False  # §3: navigation ran off the end under an
    # in-flight interrupt; re-entry finalizes via _complete (no dispatch/wipe).
    reincarnations: dict[str, int] = field(default_factory=dict)  # per-session
    # (agent_id-keyed) proactive-handoff counter; the loop guard caps it.
    max_reincarnations: int = 2  # per-session reincarnation cap (the loop guard);
    # set from config.context_max_reincarnations() at run start.
    clean_tree_bounces: dict[str, int] = field(default_factory=dict)  # per-step
    # (step-keyed) dirty-worktree bounce counter; the enforcement hook caps it.
    max_clean_tree_bounces: int = 3  # dirty-worktree bounce cap before pausing.

    @classmethod
    def load(cls, state_dir: Path) -> State:
        from imp.providers.config import ProviderModel  # local: avoid import cycle
        state_path = state_dir / "state.json"
        raw = _read_state_bytes(state_path)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            # Transient read-path fault, not corruption — see imp.engine.readfault.
            data = _recover_or_raise(state_path, raw, e)
        cur = data.pop("current_step", None)
        lane_restore = data.pop("lane_restore", None)
        raw_rt = data.pop("resolved_tiers", None)
        # Preserve None (not yet resolved) vs a dict (resolved). A `... or {}`
        # would map null→{}, defeating the executor's `is None` compute-if-absent
        # check. None still avoids the `.items()`-on-None crash because we guard.
        resolved_tiers = (
            {tier: ProviderModel(**pm) for tier, pm in raw_rt.items()}
            if raw_rt is not None else None
        )
        raw_rs = data.pop("resolved_steps", None)
        resolved_steps = (
            {step: ProviderModel(**pm) for step, pm in raw_rs.items()}
            if raw_rs is not None else None
        )
        # Drop keys for fields removed since this state.json was written (e.g.
        # pending_rename/drafting/random_title from the name-on-intent rework) so
        # an old paused/running quest can still be loaded, abandoned, or resumed.
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in data.items() if k in known}
        if "status" in data:
            data["status"] = _LEGACY_STATUS.get(data["status"], data["status"])
        return cls(
            **data,
            current_step=CurrentStep.from_dict(cur) if cur else None,
            resolved_tiers=resolved_tiers,
            resolved_steps=resolved_steps,
            lane_restore=(
                LaneRestore.from_dict(lane_restore) if lane_restore else None),
        )

    def save(self, state_dir: Path) -> None:
        """Atomic write: tmp file in same dir, fsync, rename.

        Sibling implementation: agents/tools/fs.py _atomic_write_bytes
        (edit_file's write-back). The differences are DELIBERATE, not drift:
        this one fsyncs (state.json is the workflow's source of truth with no
        git recovery; edit_file waives durability — the repo recovers) and
        skips the mode copy (state.json has no exec bit to preserve). The
        BaseException cleanup net is shared: a cancellation mid-save must not
        leak a temp file."""
        state_dir.mkdir(parents=True, exist_ok=True)
        target = state_dir / "state.json"
        payload = {
            "id": self.id,
            "step": self.step,
            "step_label": self.step_label,
            "phase_label": self.phase_label,
            "route_label": self.route_label,
            "started_at": self.started_at,
            "step_attempt": self.step_attempt,
            "status": self.status,
            "halt_condition": self.halt_condition,
            "has_started": self.has_started,
            "vars": self.vars,
            "loop_counts": self.loop_counts,
            "outcome_history": self.outcome_history,
            "current_step": self.current_step.to_dict() if self.current_step else None,
            "retry_feedback": self.retry_feedback,
            "resume_pending": self.resume_pending,
            "resume_target": self.resume_target,
            "resume_should_continue": self.resume_should_continue,
            "resolved_tiers": (
                {t: {"provider": pm.provider, "model": pm.model}
                 for t, pm in self.resolved_tiers.items()}
                if self.resolved_tiers is not None else None
            ),
            "resolved_steps": (
                {s: {"provider": pm.provider, "model": pm.model}
                 for s, pm in self.resolved_steps.items()}
                if self.resolved_steps is not None else None
            ),
            "memory": self.memory,
            "merge_commit": self.merge_commit,
            "held_lane": self.held_lane,
            "held_acquire_step": self.held_acquire_step,
            "lane_retained": self.lane_retained,
            "lane_restore": (
                self.lane_restore.to_dict() if self.lane_restore else None),
            "unsandboxed": self.unsandboxed,
            "completion_pending": self.completion_pending,
            "reincarnations": self.reincarnations,
            "max_reincarnations": self.max_reincarnations,
            "clean_tree_bounces": self.clean_tree_bounces,
            "max_clean_tree_bounces": self.max_clean_tree_bounces,
        }
        fd, tmp_name = tempfile.mkstemp(prefix=".state-", dir=state_dir)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)
            raise

    def arm_step_resume(
        self, *, should_continue: bool, cause: ResumeCause = "human",
        target: ResumeTarget | None = None,
    ) -> None:
        """Sole writer of the (pending, should_continue, cause) resume triple and its landing."""
        self.resume_pending = self.step
        self.resume_should_continue = should_continue
        self.resume_cause = cause
        self.resume_target = target

    def clear_step_resume(self) -> None:
        """Sole clear writer: drop the triple and its landing together so they can never tear."""
        self.resume_pending = None
        self.resume_should_continue = False
        self.resume_cause = "human"
        self.resume_target = None

    def reincarnation_count(self, key: str) -> int:
        return self.reincarnations.get(key, 0)

    def bump_reincarnation(self, key: str) -> int:
        n = self.reincarnations.get(key, 0) + 1
        self.reincarnations[key] = n
        return n

    def reset_reincarnation(self, key: str) -> None:
        self.reincarnations.pop(key, None)

    def clean_tree_bounce_count(self, key: str) -> int:
        return self.clean_tree_bounces.get(key, 0)

    def bump_clean_tree_bounce(self, key: str) -> int:
        n = self.clean_tree_bounces.get(key, 0) + 1
        self.clean_tree_bounces[key] = n
        return n


# ── EventLog: audit trail with envelope + typed appends + chain map ──────────

_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "step_context": ("provider", "model",
                     "tools_schema", "params", "max_tool_calls"),
    "user_message": ("role", "content", "subtype"),
    "engine_message": ("role", "content", "subtype"),
    # polymorphic deliverable payload — no fixed required keys; () is intentional
    "agent_result": (),
    "agent_message": ("provider", "model", "raw", "usage", "stop_reason"),
    "tool_call": ("id", "name", "input"),
    "tool_result": ("id", "content", "error"),
    "skill_load": ("name", "source"),
    "quota_reading": ("provider", "plan", "windows", "raw"),
}


def _quota_key(payload: Any) -> tuple[tuple[Any, Any, Any], ...]:
    """What "changed" means for a reading: any window's percentage or reset."""
    windows = payload.get("windows") if isinstance(payload, dict) else None
    if not isinstance(windows, list):
        return ()
    return tuple((w.get("label"), w.get("used_pct"), w.get("resets_at"))
                 for w in windows if isinstance(w, dict))


def _tool_result_content_for_event(content: Any) -> Any:
    """Project a tool_result's content for events.jsonl: lean blob refs, no base64."""
    if isinstance(content, str):
        return content
    out: list[dict[str, Any]] = []
    for p in content:
        if isinstance(p, dict):
            out.append(p)  # native Claude SDK content block; verbatim
        elif getattr(p, "type", None) == "image":
            out.append({"type": "image", "blob": p.blob,
                        "media_type": p.media_type,
                        "w": p.width, "h": p.height, "bytes": p.nbytes})
        else:
            out.append({"type": "text", "text": p.text})
    return out


def _block_to_dict(b: Any) -> dict[str, Any]:
    """Serialize a content block by duck-typing on .type (avoids importing the
    runner block classes into state.py — keeps the module dependency-free)."""
    bt = getattr(b, "type", None)
    if bt == "text":
        return {"type": "text", "text": b.text}
    if bt == "tool_use":
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    if bt == "tool_result":
        return {"type": "tool_result", "tool_use_id": b.tool_use_id,
                "content": _tool_result_content_for_event(b.content),
                "is_error": b.is_error}
    return {"type": str(bt)}


@dataclass(frozen=True)
class ResponseShape:
    """What one model response held, summed over all of its agent_message events."""
    thinking_blocks: int
    has_text: bool


@dataclass(frozen=True)
class _TurnResponses:
    before: ResponseShape | None
    last: ResponseShape
    last_message_id: str | None


def _response_shape(raw: Any) -> ResponseShape:
    # Replay feeds this every raw a journal ever recorded, so no shape may raise.
    content = raw.get("content") if isinstance(raw, dict) else None
    blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
    return ResponseShape(
        thinking_blocks=sum(b.get("type") == "thinking" for b in blocks),
        has_text=any(b.get("type") == "text" and str(b.get("text") or "").strip()
                     for b in blocks))


@runtime_checkable
class EventLog(Protocol):
    """Audit log. Callers never pass parent_id; the writer computes it from an
    internal agent_id → last_event_id chain map. parent_id_override is honored
    only on an agent's first append (its step_context)."""

    def append_step_context(self, *, agent_id: str | None,
                            parent_id_override: str | None = None,
                            provider: str, model: str,
                            tools_schema: Any, params: dict[str, Any],
                            max_tool_calls: int | None) -> dict[str, Any]: ...
    def append_user_message(self, *, agent_id: str | None,
                            message: Message, subtype: str) -> dict[str, Any]: ...
    def append_engine_message(self, *, agent_id: str | None,
                              message: Any, subtype: str) -> dict[str, Any]: ...
    def append_agent_message(self, *, agent_id: str | None, provider: str,
                             model: str, raw: Any, usage: dict[str, Any],
                             stop_reason: Any,
                             auth_mode: str | None = None,
                             provider_error: bool = False,
                             handoff: bool = False,
                             context_window: int | None = None,
                             context_tokens: int | None = None,
                             message_id: str | None = None,
                             request_id: str | None = None,
                             duration_ms: float | None = None) -> dict[str, Any]: ...
    def append_tool_call(self, *, agent_id: str | None,
                         block: Any, label: str | None = None,
                         id_ns: str | None = None) -> dict[str, Any]: ...
    def append_tool_result(self, *, agent_id: str | None,
                           block: Any,
                           citations: list[dict[str, Any]] | None = None,
                           id_ns: str | None = None,
                           kind: str | None = None,
                           duration_ms: float | None = None,
                           ) -> dict[str, Any]: ...
    def append_query_result(self, *, agent_id: str | None,
                            session_id: str,
                            total_cost_usd: float | None,
                            duration_ms: int | None,
                            duration_api_ms: int | None,
                            num_turns: int | None,
                            stop_reason: Any,
                            subtype: str | None,
                            is_error: bool,
                            usage: dict[str, Any],
                            model_usage: dict[str, Any],
                            api_error_status: Any = None,
                            permission_denials: Any = None,
                            errors: Any = None) -> dict[str, Any]: ...
    def append(self, *, agent_id: str | None, type: str,
               payload: dict[str, Any]) -> dict[str, Any]: ...
    def append_stderr_warning(self, *, agent_id: str | None,
                              message: str) -> None: ...
    def append_quota_reading(self, *, agent_id: str | None,
                             reading: QuotaReading) -> None: ...
    def read(self) -> Iterator[dict[str, Any]]: ...
    def tail(self, n: int) -> list[dict[str, Any]]: ...
    def response_before_last(self, agent_id: str | None) -> ResponseShape | None: ...
    def resend_outstanding(self, agent_id: str | None) -> bool: ...
    def update_last_agent_message_usage(
        self, *, agent_id: str, usage: dict[str, Any],
        context_tokens: int | None = None, context_window: int | None = None,
    ) -> None: ...


class _BaseEventLog:
    """Shared envelope/chain/validation/fan-out logic. Subclasses implement
    _persist(record) and read()."""

    def __init__(self, *, on_event: Any = None, strict: bool = False) -> None:
        self._on_event = on_event
        self._strict = strict
        self._last: dict[str, str] = {}   # agent_id → last event id (non-None only)
        self._seen_stderr_warnings: set[str] = set()
        self._last_quota: dict[str, tuple[tuple[Any, Any, Any], ...]] = {}
        self._turn_responses: dict[str, _TurnResponses] = {}
        self._resend_outstanding: set[str] = set()
        self._rebuild_from_history()

    def _rebuild_from_history(self) -> None:
        for e in self.read():
            aid = e.get("agent_id")
            eid = e.get("id")
            if aid is not None and isinstance(eid, str):
                self._last[aid] = eid
            payload = e.get("payload")
            if isinstance(aid, str):
                self._track_response(aid, e.get("type"), payload)
            if e.get("type") == "runner_warning" and isinstance(payload, dict):
                message = payload.get("message")
                if isinstance(message, str):
                    self._seen_stderr_warnings.add(message)
            if e.get("type") == "quota_reading" and isinstance(payload, dict):
                self._last_quota[str(payload.get("provider"))] = _quota_key(payload)

    def _track_response(self, agent_id: str, event_type: Any, payload: Any) -> None:
        if event_type in ("user_message", "engine_message"):
            self._turn_responses.pop(agent_id, None)
            subtype = payload.get("subtype") if isinstance(payload, dict) else None
            if subtype == "undelivered_message":
                self._resend_outstanding.add(agent_id)
            elif subtype != "continuation":
                self._resend_outstanding.discard(agent_id)
        elif event_type == "awaiting_user_input":
            self._resend_outstanding.discard(agent_id)
        elif event_type == "agent_message" and isinstance(payload, dict):
            shape = _response_shape(payload.get("raw"))
            message_id = payload.get("message_id")
            turn = self._turn_responses.get(agent_id)
            if turn is None:
                self._turn_responses[agent_id] = _TurnResponses(None, shape, message_id)
            elif message_id is not None and message_id == turn.last_message_id:
                merged = ResponseShape(turn.last.thinking_blocks + shape.thinking_blocks,
                                       turn.last.has_text or shape.has_text)
                self._turn_responses[agent_id] = _TurnResponses(turn.before, merged, message_id)
            else:
                self._turn_responses[agent_id] = _TurnResponses(turn.last, shape, message_id)

    def response_before_last(self, agent_id: str | None) -> ResponseShape | None:
        """The agent's response before its latest, when both are in one turn; reads no log."""
        turn = self._turn_responses.get(agent_id) if agent_id is not None else None
        return turn.before if turn is not None else None

    def resend_outstanding(self, agent_id: str | None) -> bool:
        """Whether a correction went to this agent and it has not yielded or heard a user since."""
        return agent_id is not None and agent_id in self._resend_outstanding

    # --- core emit -----------------------------------------------------------

    def _emit(self, *, agent_id: str | None, type: str, payload: Any,
              parent_id_override: str | None = None) -> dict[str, Any]:
        new_id = uuid.uuid4().hex
        if parent_id_override is not None:
            parent_id = parent_id_override
        elif agent_id is None:
            parent_id = None                 # executor-level events never chain
        else:
            parent_id = self._last.get(agent_id)

        issues = self._validate(type, payload)
        if issues:
            if self._strict:
                raise ValueError(f"{type} payload invalid: {'; '.join(issues)}")
            payload = self._coerce(payload)

        record = {
            "id": new_id,
            "parent_id": parent_id,
            "agent_id": agent_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "type": type,
            "payload": payload,
        }
        # persist before broadcast with no await; replay uses disk-before-live as partition
        self._persist(record)
        if agent_id is not None:
            self._last[agent_id] = new_id
            self._track_response(agent_id, type, payload)
        if self._on_event is not None:
            self._on_event(record)

        # Write-side failure contract: in lenient mode, surface validation
        # issues as a runner_warning AFTER the best-effort write. runner_warning
        # has a trivial shape so it can never re-trigger this branch.
        if issues and not self._strict:
            self._emit(agent_id=agent_id, type="runner_warning",
                       payload={"message": f"{type} payload invalid: "
                                           f"{'; '.join(issues)}"})
        return record

    @staticmethod
    def _validate(event_type: str, payload: Any) -> list[str]:
        # NOTE: parameter is `event_type`, NOT `type` — shadowing the builtin
        # `type` here would break the `payload.__class__.__name__` path below.
        if not isinstance(payload, dict):
            return [f"payload is {payload.__class__.__name__}, expected dict"]
        missing = [k for k in _REQUIRED_KEYS.get(event_type, ()) if k not in payload]
        return [f"missing key {k!r}" for k in missing]

    @staticmethod
    def _coerce(payload: Any) -> dict[str, Any]:
        return payload if isinstance(payload, dict) else {"_raw": str(payload)}

    # --- typed appends -------------------------------------------------------

    def append_step_context(self, *, agent_id: str | None,
                            parent_id_override: str | None = None,
                            provider: str, model: str,
                            tools_schema: Any, params: dict[str, Any],
                            max_tool_calls: int | None) -> dict[str, Any]:
        return self._emit(
            agent_id=agent_id, type="step_context",
            parent_id_override=parent_id_override,
            payload={"provider": provider, "model": model,
                     "tools_schema": tools_schema, "params": params,
                     "max_tool_calls": max_tool_calls})

    def append_user_message(self, *, agent_id: str | None,
                            message: Any, subtype: str) -> dict[str, Any]:
        return self._emit(
            agent_id=agent_id, type="user_message",
            payload={"role": message.role,
                     "content": [_block_to_dict(b) for b in message.content],
                     "subtype": subtype})

    def append_engine_message(self, *, agent_id: str | None,
                              message: Any, subtype: str) -> dict[str, Any]:
        return self._emit(
            agent_id=agent_id, type="engine_message",
            payload={"role": message.role,
                     "content": [_block_to_dict(b) for b in message.content],
                     "subtype": subtype})

    def append_agent_message(self, *, agent_id: str | None, provider: str,
                             model: str, raw: Any, usage: dict[str, Any],
                             stop_reason: Any,
                             auth_mode: str | None = None,
                             provider_error: bool = False,
                             handoff: bool = False,
                             context_window: int | None = None,
                             context_tokens: int | None = None,
                             message_id: str | None = None,
                             request_id: str | None = None,
                             duration_ms: float | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "provider": provider, "model": model, "raw": raw,
            "usage": usage, "stop_reason": stop_reason,
        }
        if auth_mode is not None:
            payload["auth_mode"] = auth_mode
        if provider_error:
            payload["provider_error"] = True
        if handoff:
            payload["handoff"] = True
        if context_window is not None:
            payload["context_window"] = context_window
        if context_tokens is not None:
            payload["context_tokens"] = context_tokens
        if message_id is not None:
            payload["message_id"] = message_id
        if request_id is not None:
            payload["request_id"] = request_id
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        return self._emit(
            agent_id=agent_id, type="agent_message", payload=payload)

    def append_tool_call(self, *, agent_id: str | None,
                         block: Any, label: str | None = None,
                         id_ns: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": block.id, "name": block.name, "input": block.input}
        if label is not None:
            payload["label"] = label
        # §3 marker for a `task` dispatch that will be closed by an authoritative
        # `id_ns`-stamped tool_result (the block-close protocol). Mirrors the same
        # field on append_tool_result so a dispatch/result pair is symmetrically
        # tagged. The renderer reads it to keep the subagent fold OPEN until that
        # result lands (instead of the opener's next event resume-closing it with a
        # near-instant end_ts). Absent on legacy dispatches with no such result.
        if id_ns is not None:
            payload["id_ns"] = id_ns
        return self._emit(agent_id=agent_id, type="tool_call", payload=payload)

    def append_tool_result(self, *, agent_id: str | None,
                           block: Any,
                           citations: list[dict[str, Any]] | None = None,
                           id_ns: str | None = None,
                           kind: str | None = None,
                           duration_ms: float | None = None,
                           ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": block.tool_use_id,
            "content": _tool_result_content_for_event(block.content),
            "error": block.is_error}
        # Optional, web-only enrichment. Absent on every non-web result so the
        # `tool_result` shape and _REQUIRED_KEYS stay unchanged. Empty list is
        # treated as "no citations" — never write an empty array.
        if citations:
            payload["citations"] = citations
        # §3 fold-probe namespace marker for task results ("block"); absent on legacy
        # and non-task results so the ordered two-dict probe still fires.
        if id_ns is not None:
            payload["id_ns"] = id_ns
        if kind is not None:
            payload["kind"] = kind
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        return self._emit(agent_id=agent_id, type="tool_result", payload=payload)

    def append_query_result(self, *, agent_id: str | None,
                            session_id: str,
                            total_cost_usd: float | None,
                            duration_ms: int | None,
                            duration_api_ms: int | None,
                            num_turns: int | None,
                            stop_reason: Any,
                            subtype: str | None,
                            is_error: bool,
                            usage: dict[str, Any],
                            model_usage: dict[str, Any],
                            api_error_status: Any = None,
                            permission_denials: Any = None,
                            errors: Any = None) -> dict[str, Any]:
        # session_id spans one SDK client lifetime; the row is cumulative in it.
        payload: dict[str, Any] = {
            "session_id": session_id,
            "total_cost_usd": total_cost_usd, "duration_ms": duration_ms,
            "duration_api_ms": duration_api_ms, "num_turns": num_turns,
            "stop_reason": stop_reason, "subtype": subtype,
            "is_error": is_error, "usage": usage, "model_usage": model_usage,
        }
        if api_error_status is not None:
            payload["api_error_status"] = api_error_status
        if permission_denials is not None:
            payload["permission_denials"] = permission_denials
        if errors is not None:
            payload["errors"] = errors
        return self._emit(
            agent_id=agent_id, type="query_result", payload=payload)

    def append(self, *, agent_id: str | None, type: str,
               payload: Any) -> dict[str, Any]:
        return self._emit(agent_id=agent_id, type=type, payload=payload)

    def append_stderr_warning(self, *, agent_id: str | None,
                              message: str) -> None:
        """Dedup run-wide: one sink per agent step, so an unchanging warning would repeat."""
        if message in self._seen_stderr_warnings:
            return
        self._seen_stderr_warnings.add(message)
        self._emit(agent_id=agent_id, type="runner_warning",
                   payload={"message": message})

    def append_quota_reading(self, *, agent_id: str | None,
                             reading: QuotaReading) -> None:
        """Change-only, scoped to this quest's log: codex reports the same
        account-wide level every turn, and a resumed step re-reads the one
        already recorded. Per provider, so one carrier never masks another."""
        payload = reading.to_payload()
        key = _quota_key(payload)
        if self._last_quota.get(reading.provider) == key:
            return
        self._last_quota[reading.provider] = key
        self._emit(agent_id=agent_id, type="quota_reading", payload=payload)

    # --- abstract ------------------------------------------------------------

    def _persist(self, record: dict[str, Any]) -> None:
        raise NotImplementedError

    def read(self) -> Iterator[dict[str, Any]]:
        raise NotImplementedError


def parse_events_jsonl(raw: bytes) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"_malformed": True, "raw": line})
    return out


class JsonlEventLog(_BaseEventLog):
    """File-backed append-only event log."""

    def __init__(self, path: Path, *, on_event: Any = None,
                 strict: bool = False) -> None:
        self.path = path
        super().__init__(on_event=on_event, strict=strict)

    def _persist(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(record))
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())

    def read(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    yield {"_malformed": True, "raw": line}

    def tail(self, n: int) -> list[dict[str, Any]]:
        if n <= 0 or not self.path.exists():
            return []
        chunk = 4096
        with self.path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            data = b""
            # Read backward until we have captured more than n line breaks (so the
            # n trailing records are complete) or reach the start of the file.
            while pos > 0 and data.count(b"\n") <= n:
                step = min(chunk, pos)
                pos -= step
                f.seek(pos)
                data = f.read(step) + data
        out: list[dict[str, Any]] = []
        for line in data.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                out.append({"_malformed": True, "raw": line})
        return out[-n:]

    def update_last_agent_message_usage(
        self, *, agent_id: str, usage: dict[str, Any],
        context_tokens: int | None = None, context_window: int | None = None,
    ) -> None:
        # JsonlEventLog is append-only on disk. Write a usage-patch row so
        # read_telemetry can fold it into the preceding agent_message totals.
        # NOT a second agent_message — that would double-count under telemetry's
        # one-response-per-turn invariant.
        payload: dict[str, Any] = {"usage": usage}
        if context_tokens is not None:
            payload["context_tokens"] = context_tokens
        if context_window is not None:
            payload["context_window"] = context_window
        record: dict[str, Any] = {
            "type": "agent_message_usage_patch",
            "agent_id": agent_id,
            "payload": payload,
        }
        # broadcast as persisted: no _emit envelope, so on-disk shape is unchanged
        self._persist(record)
        if self._on_event is not None:
            self._on_event(record)

    def events_for_current_step(self, step_name: str) -> Iterable[dict[str, Any]]:
        """Replay-helper for `imp resume`: yield events from the last
        `step_started` for `step_name` until the next different step starts.
        Used to show context when resuming.
        """
        events = list(self.read())
        last_start = -1
        for i, e in enumerate(events):
            if (e.get("type") == "step_started"
                    and (e.get("payload") or {}).get("step") == step_name):
                last_start = i
        if last_start == -1:
            return []
        end = len(events)
        for i in range(last_start + 1, len(events)):
            if (events[i].get("type") == "step_started"
                    and (events[i].get("payload") or {}).get("step") != step_name):
                end = i
                break
        return events[last_start:end]


class InMemoryEventLog(_BaseEventLog):
    """In-memory event log for tests; shape-equivalent to JsonlEventLog."""

    def __init__(self, *, on_event: Any = None, strict: bool = False) -> None:
        self._events: list[dict[str, Any]] = []
        super().__init__(on_event=on_event, strict=strict)

    def _persist(self, record: dict[str, Any]) -> None:
        self._events.append(record)

    def read(self) -> Iterator[dict[str, Any]]:
        yield from list(self._events)

    def tail(self, n: int) -> list[dict[str, Any]]:
        if n <= 0:
            return []
        return list(self._events[-n:])

    def update_last_agent_message_usage(
        self, *, agent_id: str, usage: dict[str, Any],
        context_tokens: int | None = None, context_window: int | None = None,
    ) -> None:
        for ev in reversed(self._events):
            if ev.get("type") == "agent_message" and ev.get("agent_id") == agent_id:
                payload = ev.setdefault("payload", {})
                existing = payload.get("usage") or {}
                # Merge: codex emits full usage at turn.completed; the prior
                # agent_message (from item:agent_message) carries no usage.
                payload["usage"] = {**existing, **usage}
                if context_tokens is not None:
                    payload["context_tokens"] = context_tokens
                if context_window is not None:
                    payload["context_window"] = context_window
                return
        raise RuntimeError(
            f"no prior agent_message for agent_id={agent_id!r} to attach usage to"
        )


class LockHeld(Exception):
    """Raised when a lock could not be acquired before the timeout."""

    def __init__(self, path: Path, held_by_pid: int | None) -> None:
        self.path = path
        self.held_by_pid = held_by_pid
        super().__init__(
            f"lock at {path} held by pid {held_by_pid}"
            if held_by_pid
            else f"lock at {path} is held"
        )


class ProjectLock:
    """File-backed advisory lock for single-quest-running enforcement.

    The lock file's content is the holder's PID so `imp show` can display it.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fl = filelock.FileLock(str(path) + ".filelock")

    @contextmanager
    def acquire(self, timeout: float = 0.0):  # type: ignore[no-untyped-def]
        try:
            self._fl.acquire(timeout=timeout)
        except filelock.Timeout as e:
            held_pid = self._read_pid()
            raise LockHeld(self.path, held_pid) from e
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(str(os.getpid()))
            yield self
        finally:
            try:
                self.path.unlink(missing_ok=True)
            finally:
                self._fl.release()

    def _read_pid(self) -> int | None:
        try:
            return int(self.path.read_text().strip())
        except (FileNotFoundError, ValueError):
            return None


def lock_is_live(lock_path: Path) -> bool:
    """True iff `lock_path` exists and names a living pid.

    The single pid-liveness probe in the codebase — the display resolver and the
    daemon crash snapshot both call it so they can't drift. Signal-0 probe:
    ESRCH (no such process) ⇒ dead; EPERM (exists, not ours) ⇒ alive. A live
    run's pid is never falsely read as dead (the safe direction)."""
    try:
        pid = int(lock_path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

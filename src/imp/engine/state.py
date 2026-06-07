"""Per-Change persistence: state.json (cursor) + events.jsonl (audit log) + locks.

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
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from imp.agents.runner import Message
    from imp.providers.config import ProviderModel

import filelock

Status = Literal[
    "running", "paused", "completed", "errored", "merge_blocked", "abandoned"
]


@dataclass
class CurrentStep:
    transcript_dir: Path

    def to_dict(self) -> dict[str, Any]:
        return {"transcript_dir": str(self.transcript_dir)}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CurrentStep:
        return cls(transcript_dir=Path(raw["transcript_dir"]))


@dataclass
class State:
    id: str
    step: str | None
    step_attempt: int
    status: Status
    vars: dict[str, Any] = field(default_factory=dict)
    loop_counts: dict[str, int] = field(default_factory=dict)
    current_step: CurrentStep | None = None
    retry_feedback: dict[str, Any] | None = None
    resume_pending: str | None = None
    resolved_tiers: dict[str, ProviderModel] | None = None
    resolved_steps: dict[str, ProviderModel] | None = None

    @classmethod
    def load(cls, state_dir: Path) -> State:
        from imp.providers.config import ProviderModel  # local: avoid import cycle
        data = json.loads((state_dir / "state.json").read_text())
        cur = data.pop("current_step", None)
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
        return cls(
            **data,
            current_step=CurrentStep.from_dict(cur) if cur else None,
            resolved_tiers=resolved_tiers,
            resolved_steps=resolved_steps,
        )

    def save(self, state_dir: Path) -> None:
        """Atomic write: tmp file in same dir, fsync, rename."""
        state_dir.mkdir(parents=True, exist_ok=True)
        target = state_dir / "state.json"
        payload = {
            "id": self.id,
            "step": self.step,
            "step_attempt": self.step_attempt,
            "status": self.status,
            "vars": self.vars,
            "loop_counts": self.loop_counts,
            "current_step": self.current_step.to_dict() if self.current_step else None,
            "retry_feedback": self.retry_feedback,
            "resume_pending": self.resume_pending,
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
        }
        fd, tmp_name = tempfile.mkstemp(prefix=".state-", dir=state_dir)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, target)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)
            raise


# ── EventLog: audit trail with envelope + typed appends + chain map ──────────

_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "step_context": ("provider", "model", "system_prompt",
                     "tools_schema", "params", "max_tool_calls"),
    "user_message": ("role", "content"),
    "llm_response": ("provider", "model", "raw", "usage", "stop_reason"),
    "tool_call": ("id", "name", "input"),
    "tool_result": ("id", "content", "error"),
    "skill_load": ("name", "source"),
}


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
                "content": b.content, "is_error": b.is_error}
    return {"type": str(bt)}


@runtime_checkable
class EventLog(Protocol):
    """Audit log. Callers never pass parent_id; the writer computes it from an
    internal agent_id → last_event_id chain map. parent_id_override is honored
    only on an agent's first append (its step_context)."""

    def append_step_context(self, *, agent_id: str | None,
                            parent_id_override: str | None = None,
                            provider: str, model: str, system_prompt: Any,
                            tools_schema: Any, params: dict[str, Any],
                            max_tool_calls: int | None) -> dict[str, Any]: ...
    def append_user_message(self, *, agent_id: str | None,
                            message: Message) -> dict[str, Any]: ...
    def append_llm_response(self, *, agent_id: str | None, provider: str,
                            model: str, raw: Any, usage: dict[str, Any],
                            stop_reason: Any,
                            auth_mode: str | None = None) -> dict[str, Any]: ...
    def append_tool_call(self, *, agent_id: str | None,
                         block: Any) -> dict[str, Any]: ...
    def append_tool_result(self, *, agent_id: str | None,
                           block: Any,
                           citations: list[dict[str, Any]] | None = None,
                           ) -> dict[str, Any]: ...
    def append(self, *, agent_id: str | None, type: str,
               payload: dict[str, Any]) -> dict[str, Any]: ...
    def read(self) -> Iterator[dict[str, Any]]: ...
    def update_last_llm_response_usage(
        self, *, agent_id: str, usage: dict[str, Any],
    ) -> None: ...


class _BaseEventLog:
    """Shared envelope/chain/validation/fan-out logic. Subclasses implement
    _persist(record) and read()."""

    def __init__(self, *, on_event: Any = None, strict: bool = False) -> None:
        self._on_event = on_event
        self._strict = strict
        self._last: dict[str, str] = {}   # agent_id → last event id (non-None only)
        self._rebuild_chain()

    def _rebuild_chain(self) -> None:
        for e in self.read():
            aid = e.get("agent_id")
            eid = e.get("id")
            if aid is not None and isinstance(eid, str):
                self._last[aid] = eid

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
        self._persist(record)
        if agent_id is not None:
            self._last[agent_id] = new_id
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
                            provider: str, model: str, system_prompt: Any,
                            tools_schema: Any, params: dict[str, Any],
                            max_tool_calls: int | None) -> dict[str, Any]:
        return self._emit(
            agent_id=agent_id, type="step_context",
            parent_id_override=parent_id_override,
            payload={"provider": provider, "model": model,
                     "system_prompt": system_prompt, "tools_schema": tools_schema,
                     "params": params, "max_tool_calls": max_tool_calls})

    def append_user_message(self, *, agent_id: str | None,
                            message: Any) -> dict[str, Any]:
        return self._emit(
            agent_id=agent_id, type="user_message",
            payload={"role": message.role,
                     "content": [_block_to_dict(b) for b in message.content]})

    def append_llm_response(self, *, agent_id: str | None, provider: str,
                            model: str, raw: Any, usage: dict[str, Any],
                            stop_reason: Any,
                            auth_mode: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "provider": provider, "model": model, "raw": raw,
            "usage": usage, "stop_reason": stop_reason,
        }
        if auth_mode is not None:
            payload["auth_mode"] = auth_mode
        return self._emit(
            agent_id=agent_id, type="llm_response", payload=payload)

    def append_tool_call(self, *, agent_id: str | None,
                         block: Any) -> dict[str, Any]:
        return self._emit(
            agent_id=agent_id, type="tool_call",
            payload={"id": block.id, "name": block.name, "input": block.input})

    def append_tool_result(self, *, agent_id: str | None,
                           block: Any,
                           citations: list[dict[str, Any]] | None = None,
                           ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": block.tool_use_id, "content": block.content,
            "error": block.is_error}
        # Optional, web-only enrichment. Absent on every non-web result so the
        # `tool_result` shape and _REQUIRED_KEYS stay unchanged. Empty list is
        # treated as "no citations" — never write an empty array.
        if citations:
            payload["citations"] = citations
        return self._emit(agent_id=agent_id, type="tool_result", payload=payload)

    def append(self, *, agent_id: str | None, type: str,
               payload: Any) -> dict[str, Any]:
        return self._emit(agent_id=agent_id, type=type, payload=payload)

    # --- abstract ------------------------------------------------------------

    def _persist(self, record: dict[str, Any]) -> None:
        raise NotImplementedError

    def read(self) -> Iterator[dict[str, Any]]:
        raise NotImplementedError


class JsonlEventLog(_BaseEventLog):
    """File-backed append-only event log."""

    def __init__(self, path: Path, *, on_event: Any = None,
                 strict: bool = False) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(on_event=on_event, strict=strict)

    def _persist(self, record: dict[str, Any]) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps(record))
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())

    def read(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    yield {"_malformed": True, "raw": line}

    def update_last_llm_response_usage(
        self, *, agent_id: str, usage: dict[str, Any],
    ) -> None:
        # JsonlEventLog is append-only on disk. Write a usage-patch row so
        # read_telemetry can fold it into the preceding llm_response totals.
        # NOT a second llm_response — that would double-count under telemetry's
        # one-response-per-turn invariant.
        self._persist({
            "type": "llm_response_usage_patch",
            "agent_id": agent_id,
            "payload": {"usage": usage},
        })

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

    def update_last_llm_response_usage(
        self, *, agent_id: str, usage: dict[str, Any],
    ) -> None:
        for ev in reversed(self._events):
            if ev.get("type") == "llm_response" and ev.get("agent_id") == agent_id:
                payload = ev.setdefault("payload", {})
                existing = payload.get("usage") or {}
                # Merge: codex emits full usage at turn.completed; the prior
                # llm_response (from item:agent_message) carries no usage.
                payload["usage"] = {**existing, **usage}
                return
        raise RuntimeError(
            f"no prior llm_response for agent_id={agent_id!r} to attach usage to"
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
    """File-backed advisory lock for single-Change-running enforcement.

    The lock file's content is the holder's PID so `imp status` can display it.
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

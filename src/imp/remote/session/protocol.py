# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""The attach-protocol frame codec for a per-quest session daemon.

Each frame is a single JSON line. Client -> daemon frames carry a "kind" of
"attach" (the opening handshake), "input" (a gate reply), "skill" (a skill
load request), "control" (an interrupt/detach/abandon/save/archive/discard
action), "set-lease" (toggle this attachment's keep-alive lease), or "query"
(a read-only introspection query). Daemon -> client frames are "event"
(wrapping one curated engine event), "result" (the reply to a query), or
"end" (a bare marker: the run is over, disk and archive are authoritative).
Encoders return a line WITHOUT a trailing newline; the caller frames it
(matching remote/daemon/protocol.py:make_hello)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

_MAX_FRAME = 1 << 20  # hard wire cap: encode_event clips event frames to fit under it

SET_LEASE = "set-lease"

CONTROL_INTERRUPT = "interrupt"
CONTROL_INTERRUPT_LEAVE = "interrupt-leave"
CONTROL_INTERRUPT_FORCE = "interrupt-force"
CONTROL_DETACH = "detach"
CONTROL_ABANDON = "abandon"
CONTROL_SAVE = "save"
CONTROL_ARCHIVE = "archive"
CONTROL_DISCARD = "discard"
CONTROL_SET_MODEL = "set-model"
CONTROL_SET_SANDBOX = "set-sandbox"
CONTROL_SET_PROVIDER = "set-provider"
CONTROL_COMPACT = "compact"
CONTROL_RETRY_NOW = "retry-now"
_CONTROL_ACTIONS = frozenset({
    CONTROL_INTERRUPT, CONTROL_INTERRUPT_LEAVE, CONTROL_INTERRUPT_FORCE,
    CONTROL_DETACH, CONTROL_ABANDON, CONTROL_SAVE, CONTROL_ARCHIVE,
    CONTROL_DISCARD, CONTROL_SET_MODEL, CONTROL_SET_SANDBOX,
    CONTROL_SET_PROVIDER, CONTROL_COMPACT, CONTROL_RETRY_NOW,
})

# Pause reasons on the park→mutate→re-enter seam (daemon hold loop, not a runner interrupt).
SIDE_EFFECT_REASONS = frozenset({"model-override", "compact"})

# Interrupt-machinery records. Observational only: delivery can land AFTER the
# gate, so they must never move a consumer's derived run state.
INTERRUPT_EVENT_TYPES = frozenset({
    "interrupt_requested", "interrupt_delivery_started",
    "interrupt_delivered", "interrupt_escalated"})


@dataclass(frozen=True, slots=True)
class Attach:
    quest_id: str
    title: str
    lease: bool = False


@dataclass(frozen=True, slots=True)
class Input:
    text: str


@dataclass(frozen=True, slots=True)
class Control:
    action: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class SetLease:
    value: bool


@dataclass(frozen=True, slots=True)
class SkillRequest:
    name: str
    args: str


@dataclass(frozen=True, slots=True)
class Query:
    op: str
    args: dict[str, Any]


ClientMessage = Attach | Input | Control | SetLease | SkillRequest | Query

FRAME_END = "end"


@dataclass(frozen=True, slots=True)
class EndOfRun:
    """The end-of-run marker: the run is over, disk and archive are authoritative."""


END_OF_RUN = EndOfRun()


def encode_attach(quest_id: str, title: str, lease: bool = False) -> str:
    return json.dumps(
        {"kind": "attach", "quest_id": quest_id, "title": title, "lease": lease})


def encode_set_lease(value: bool) -> str:
    return json.dumps({"kind": SET_LEASE, "value": value})


def encode_input(text: str) -> str:
    return json.dumps({"kind": "input", "text": text})


def encode_skill_request(name: str, args: str) -> str:
    return json.dumps({"kind": "skill", "name": name, "args": args})


def encode_control(action: str, reason: str | None = None) -> str:
    if action not in _CONTROL_ACTIONS:
        raise ValueError(f"unknown control action {action!r}")
    payload: dict[str, Any] = {"kind": "control", "action": action}
    if reason is not None:
        payload["reason"] = reason
    return json.dumps(payload)


def encode_query(op: str, args: dict[str, Any]) -> str:
    return json.dumps({"kind": "query", "op": op, "args": args})


def encode_result(value: Any) -> str:
    return json.dumps({"kind": "result", "ok": True, "value": value})


def encode_result_error(message: str) -> str:
    return json.dumps({"kind": "result", "ok": False, "error": message})


def encode_end() -> str:
    return json.dumps({"kind": FRAME_END})


def _clip_strings(value: Any, limit: int) -> Any:
    """Shrink string leaves to `limit` chars; keys, nesting and non-strings are untouched."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "… [clipped]"
    if isinstance(value, dict):
        return {k: _clip_strings(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip_strings(v, limit) for v in value]
    return value


def encode_event(event: dict[str, Any]) -> str:
    budget = _MAX_FRAME - 1  # the caller appends the newline
    line = json.dumps({"kind": "event", "event": event})
    limit = budget
    # JSON escaping makes a clip's byte saving unknowable up front, so halve until it fits.
    while len(line.encode("utf-8")) > budget and limit > 0:
        limit //= 2
        line = json.dumps({"kind": "event", "event": _clip_strings(event, limit)})
    return line


def parse_client_message(line: str) -> ClientMessage:
    obj = json.loads(line)
    if not isinstance(obj, dict):
        raise ValueError(f"expected JSON object, got {type(obj).__name__}")
    kind = obj.get("kind")
    try:
        if kind == "attach":
            return Attach(obj["quest_id"], obj["title"], bool(obj.get("lease", False)))
        if kind == "input":
            return Input(obj["text"])
        if kind == "skill":
            return SkillRequest(obj["name"], obj["args"])
        if kind == SET_LEASE:
            return SetLease(bool(obj["value"]))
        if kind == "control":
            action = obj["action"]
            if action not in _CONTROL_ACTIONS:
                raise ValueError(f"unknown control action {action!r}")
            return Control(action, obj.get("reason"))
        if kind == "query":
            return Query(obj["op"], dict(obj.get("args") or {}))
    except KeyError as exc:
        raise ValueError(f"missing required field {exc}") from exc
    raise ValueError(f"unknown client message kind {kind!r}")


def _as_object(line: str) -> dict[str, Any]:
    obj = json.loads(line)
    if not isinstance(obj, dict):
        raise ValueError(f"expected JSON object, got {type(obj).__name__}")
    return obj


def _event_of(obj: dict[str, Any]) -> dict[str, Any]:
    if obj.get("kind") != "event":
        raise ValueError(f"expected event frame, got {obj.get('kind')!r}")
    try:
        event = obj["event"]
    except KeyError:
        raise ValueError("missing required field 'event'") from None
    if not isinstance(event, dict):
        raise ValueError(f"expected 'event' to be a JSON object, got {type(event).__name__}")
    return event


def parse_event(line: str) -> dict[str, Any]:
    """Decoder pair for encode_event; stream readers should use parse_stream_frame instead."""
    return _event_of(_as_object(line))


def parse_stream_frame(line: str) -> dict[str, Any] | EndOfRun:
    """One daemon->client stream frame: the wrapped event, or the end-of-run marker."""
    obj = _as_object(line)
    if obj.get("kind") == FRAME_END:
        return END_OF_RUN
    return _event_of(obj)


def parse_result(line: str) -> Any:
    obj = json.loads(line)
    if not isinstance(obj, dict) or obj.get("kind") != "result":
        got = obj.get("kind") if isinstance(obj, dict) else type(obj).__name__
        raise ValueError(f"expected result frame, got {got!r}")
    if not obj.get("ok", False):
        raise RuntimeError(obj.get("error", "query failed"))
    return obj.get("value")

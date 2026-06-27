"""The attach-protocol frame codec for a per-change session daemon.

Each frame is a single JSON line. Client -> daemon frames carry a "kind" of
"attach" (the opening handshake), "input" (a gate reply), "control" (an
interrupt/detach/abandon/save/archive/discard action), or "set-lease" (toggle
this attachment's keep-alive lease). The daemon -> client
frame is "event", wrapping one curated engine event. Encoders return a line
WITHOUT a trailing newline; the caller frames it (matching
remote/daemon/protocol.py:make_hello)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

_MAX_FRAME = 1 << 20  # exceeding this raises DelimiterNotFound and silently detaches the client

SET_LEASE = "set-lease"

CONTROL_INTERRUPT = "interrupt"
CONTROL_DETACH = "detach"
CONTROL_ABANDON = "abandon"
CONTROL_SAVE = "save"
CONTROL_ARCHIVE = "archive"
CONTROL_DISCARD = "discard"
_CONTROL_ACTIONS = frozenset({
    CONTROL_INTERRUPT, CONTROL_DETACH, CONTROL_ABANDON, CONTROL_SAVE,
    CONTROL_ARCHIVE, CONTROL_DISCARD,
})


@dataclass(frozen=True, slots=True)
class Attach:
    change_id: str
    title: str
    lease: bool = False


@dataclass(frozen=True, slots=True)
class Input:
    text: str


@dataclass(frozen=True, slots=True)
class Control:
    action: str


@dataclass(frozen=True, slots=True)
class SetLease:
    value: bool


ClientMessage = Attach | Input | Control | SetLease


def encode_attach(change_id: str, title: str, lease: bool = False) -> str:
    return json.dumps(
        {"kind": "attach", "change_id": change_id, "title": title, "lease": lease})


def encode_set_lease(value: bool) -> str:
    return json.dumps({"kind": SET_LEASE, "value": value})


def encode_input(text: str) -> str:
    return json.dumps({"kind": "input", "text": text})


def encode_control(action: str) -> str:
    if action not in _CONTROL_ACTIONS:
        raise ValueError(f"unknown control action {action!r}")
    return json.dumps({"kind": "control", "action": action})


def encode_event(event: dict[str, Any]) -> str:
    return json.dumps({"kind": "event", "event": event})


def parse_client_message(line: str) -> ClientMessage:
    obj = json.loads(line)
    if not isinstance(obj, dict):
        raise ValueError(f"expected JSON object, got {type(obj).__name__}")
    kind = obj.get("kind")
    try:
        if kind == "attach":
            return Attach(obj["change_id"], obj["title"], bool(obj.get("lease", False)))
        if kind == "input":
            return Input(obj["text"])
        if kind == SET_LEASE:
            return SetLease(bool(obj["value"]))
        if kind == "control":
            action = obj["action"]
            if action not in _CONTROL_ACTIONS:
                raise ValueError(f"unknown control action {action!r}")
            return Control(action)
    except KeyError as exc:
        raise ValueError(f"missing required field {exc}") from exc
    raise ValueError(f"unknown client message kind {kind!r}")


def parse_event(line: str) -> dict[str, Any]:
    obj = json.loads(line)
    if not isinstance(obj, dict):
        raise ValueError(f"expected JSON object, got {type(obj).__name__}")
    if obj.get("kind") != "event":
        raise ValueError(f"expected event frame, got {obj.get('kind')!r}")
    try:
        event = obj["event"]
    except KeyError:
        raise ValueError("missing required field 'event'") from None
    if not isinstance(event, dict):
        raise ValueError(f"expected 'event' to be a JSON object, got {type(event).__name__}")
    return event

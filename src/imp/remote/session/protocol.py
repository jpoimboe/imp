"""The attach-protocol frame codec for a per-change session daemon.

Each frame is a single JSON line. Client -> daemon frames carry a "kind" of
"attach" (the opening handshake), "input" (a gate reply), or "control" (an
interrupt/detach/abandon action). The daemon -> client frame is "event", wrapping
one curated engine event. Encoders return a line WITHOUT a trailing newline; the
caller frames it (matching remote/daemon/protocol.py:make_hello)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

_MAX_FRAME = 1 << 20  # exceeding this raises DelimiterNotFound and silently detaches the client

CONTROL_INTERRUPT = "interrupt"
CONTROL_DETACH = "detach"
CONTROL_ABANDON = "abandon"
_CONTROL_ACTIONS = frozenset({CONTROL_INTERRUPT, CONTROL_DETACH, CONTROL_ABANDON})


@dataclass(frozen=True, slots=True)
class Attach:
    change_id: str
    title: str


@dataclass(frozen=True, slots=True)
class Input:
    text: str


@dataclass(frozen=True, slots=True)
class Control:
    action: str


ClientMessage = Attach | Input | Control


def encode_attach(change_id: str, title: str) -> str:
    return json.dumps({"kind": "attach", "change_id": change_id, "title": title})


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
            return Attach(obj["change_id"], obj["title"])
        if kind == "input":
            return Input(obj["text"])
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

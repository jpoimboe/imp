"""Telegram north adapter for the remote-control daemon (spec Decision 4)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from imp.remote.drivers.telegram import (
    api_call,
    format_event_message,
    send_message_params,
)

_MODERN_ID = re.compile(r"^\d{4}-\d{2}-\d{2}-([a-z0-9]{6})$")


def handle_for(change_id: str) -> str:
    """Short routing handle for a change: the 6-char suffix (modern) or the full id (legacy)."""
    m = _MODERN_ID.match(change_id)
    return m.group(1) if m else change_id


def parse_handle(text: str) -> tuple[str | None, str]:
    """Split a leading `@handle` off an inbound reply; returns (handle, rest) or (None, text)."""
    if text.startswith("@"):
        head, _, rest = text.partition(" ")
        return head[1:], rest.lstrip()
    return None, text


@dataclass(frozen=True)
class Inbound:
    text: str
    reply_to_message_id: int | None


def extract_inbound(
    updates_response: dict[str, Any], chat_id: int, offset: int
) -> tuple[list[Inbound], int]:
    """Extract allowlisted text messages from getUpdates; advance offset past every update."""
    items: list[Inbound] = []
    new_offset = offset
    for upd in updates_response.get("result", []):
        uid = upd.get("update_id")
        if isinstance(uid, int) and uid >= new_offset:
            new_offset = uid + 1
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") != chat_id:
            continue
        text = msg.get("text")
        if not (isinstance(text, str) and text):
            continue
        reply_to = msg.get("reply_to_message") or {}
        rt_id = reply_to.get("message_id")
        items.append(Inbound(
            text=text,
            reply_to_message_id=rt_id if isinstance(rt_id, int) else None))
    return items, new_offset


def resolve_change_id(
    inbound: Inbound,
    msg_to_change: dict[int, str],
    handle_to_change: dict[str, str],
) -> tuple[str | None, str]:
    if inbound.reply_to_message_id is not None:
        change_id = msg_to_change.get(inbound.reply_to_message_id)
        if change_id is not None:
            return change_id, inbound.text
    handle, rest = parse_handle(inbound.text)
    if handle is not None:
        change_id = handle_to_change.get(handle)
        if change_id is not None:
            return change_id, rest
    return None, inbound.text


def format_outbound(event: dict[str, Any], handle: str) -> str:
    """Render one event as outbound chat text, appending the reply handle."""
    return f"{format_event_message(event)}\n↩ @{handle}"


def send_message(token: str, chat_id: int, text: str, *, silent: bool) -> int | None:
    """POST one sendMessage and return the created message's id, or None if absent."""
    response = api_call(token, "sendMessage", send_message_params(
        chat_id, text, silent=silent))
    result = response.get("result") or {}
    message_id = result.get("message_id")
    return message_id if isinstance(message_id, int) else None

"""Telegram north adapter for the remote-control daemon (spec Decision 4)."""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from imp.remote.daemon.commands import Command, ListResult, Response, parse
from imp.remote.daemon.focus import Focus
from imp.remote.drivers.telegram import (
    _compose_message,
    api_call,
    clamp_text,
    curate,
    event_body,
    format_event_message,
    load_config,
    make_get_updates,
    send_message_params,
)


def handle_for(change_id: str) -> str:
    """Short routing token for a change: the bare id (dateless, already short)."""
    return change_id


def parse_handle(text: str) -> tuple[str | None, str]:
    """Split a leading `@handle` off an inbound reply; returns (handle, rest) or (None, text)."""
    if text.startswith("@"):
        head, _, rest = text.partition(" ")
        rest = rest.lstrip()
        if rest:
            return head[1:], rest
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


def format_outbound(event: dict[str, Any], handle: str, body: str | None = None) -> str:
    """Render one event as outbound chat text, appending the reply handle. A given
    `body` overrides the event's own body (e.g. an empty awaiting_user_input prompt
    that fell back to the preceding narration)."""
    message = format_event_message(event) if body is None else _compose_message(event, body)
    return f"{message}\n↩ @{handle}"


def render_response(response: Response, focused_id: str | None = None) -> str:
    if isinstance(response, ListResult):
        if not response.changes:
            return "No changes."
        lines = []
        for c in response.changes:
            mark = "● " if c.id == focused_id else ""
            lines.append(
                f"{mark}@{handle_for(c.id)}  {c.title} — {c.status} · {c.step}")
        return "\n".join(lines)
    return response.text


def send_message(token: str, chat_id: int, text: str, *, silent: bool) -> int | None:
    """POST one sendMessage and return the created message's id, or None if absent."""
    response = api_call(token, "sendMessage", send_message_params(
        chat_id, clamp_text(text), silent=silent))
    result = response.get("result") or {}
    message_id = result.get("message_id")
    return message_id if isinstance(message_id, int) else None


class TelegramNorthAdapter:
    """The daemon's single Telegram connection (spec Decision 4)."""

    def __init__(
        self,
        *,
        chat_id: int,
        submit: Callable[[Command], None],
        send: Callable[[str, bool], int | None],
        get_updates: Callable[[int], dict[str, Any]],
        focus: Focus,
    ) -> None:
        self._chat_id = chat_id
        self._submit = submit
        self._send = send
        self._get_updates = get_updates
        self._focus = focus
        self._offset = 0
        self._last_narration = ""
        self._msg_to_change: dict[int, str] = {}
        self._handle_to_change: dict[str, str] = {}
        # The reply maps below are WRITTEN by on_event (sender thread) and READ by
        # resolve (poll thread); _offset stays poll-thread-only. Guard only the maps.
        self._lock = threading.Lock()

    @classmethod
    def from_env(
        cls,
        env: dict[str, str],
        submit: Callable[[Command], None],
        *,
        focus: Focus,
        file_text: str | None = None,
    ) -> TelegramNorthAdapter:
        config = load_config(env, file_text)

        def send(text: str, silent: bool) -> int | None:
            return send_message(config.token, config.chat_id, text, silent=silent)

        get_updates = make_get_updates(config.token)

        return cls(chat_id=config.chat_id, submit=submit,
                   send=send, get_updates=get_updates, focus=focus)

    def on_event(self, event: dict[str, Any]) -> None:
        """Curate one event, send it with its handle, and record the reply maps.

        A prompt-less awaiting_user_input falls back to the preceding llm_response
        narration: "needs your input" alone is uninformative on a phone, so carry
        the last thing imp said as the body. last_narration is sender-thread-only,
        like _offset, so it needs no lock."""
        etype = event.get("type")
        kind = curate(etype)
        if kind is None:
            return
        body = event_body(etype, event.get("payload") or {})
        override: str | None = None
        if etype == "llm_response" and body.strip():
            self._last_narration = body
        elif etype == "awaiting_user_input" and not body.strip():
            body = override = self._last_narration
        if kind == "silent" and not body.strip():
            return
        change = event.get("change") or {}
        change_id = change.get("id")
        if not change_id:
            return
        handle = handle_for(change_id)
        with self._lock:
            self._handle_to_change[handle] = change_id
        if kind == "silent" and self._focus.current() != change_id:
            return
        message_id = self._send(format_outbound(event, handle, override), kind == "silent")
        with self._lock:
            if message_id is not None:
                self._msg_to_change[message_id] = change_id

    def on_response(self, response: Response) -> None:
        text = render_response(response, self._focus.current())
        if text:
            self._send(text, True)

    def resolve(self, inbound: Inbound) -> tuple[str | None, str]:
        """Resolve one inbound reply to (change_id, text) against the recorded maps."""
        with self._lock:
            return resolve_change_id(
                inbound, self._msg_to_change, self._handle_to_change)

    def _resolve_text(self, text: str, reply_to: int | None) -> tuple[str | None, str]:
        return self.resolve(Inbound(text=text, reply_to_message_id=reply_to))

    def poll_once(self) -> None:
        response = self._get_updates(self._offset)
        items, self._offset = extract_inbound(response, self._chat_id, self._offset)
        for item in items:
            self._submit(parse(item.text, item.reply_to_message_id, self._resolve_text))

"""Telegram north adapter for the remote-control daemon (spec Decision 4)."""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from imp.remote.daemon.alert_all import AlertAll
from imp.remote.daemon.commands import Command, ListResult, Notice, Response, parse
from imp.remote.daemon.subscriptions import Subscriptions
from imp.remote.drivers.telegram import (
    _compose_message,
    api_call,
    clamp_text,
    curate,
    event_body,
    format_event_message,
    header_line,
    load_config,
    make_get_updates,
    send_message_params,
    step_divider,
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


def parse_subscribe_handle(text: str) -> str | None:
    """Resolve a /subscribe arg to a change id by @handle only — handle_for is identity."""
    handle, _ = parse_handle(f"{text.strip()} .")
    return handle or None


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
    """Render one event as outbound chat text. The reply handle (↩ @handle) is
    appended ONLY for awaiting_user_input gates — the one event that asks the user
    to act; every other event renders with no handle line. A given `body` overrides
    the event's own body (the alert-all path passes the preceding narration for a
    prompt-less gate)."""
    message = format_event_message(event) if body is None else _compose_message(event, body)
    if event.get("type") == "awaiting_user_input":
        return f"{message}\n↩ @{handle}"
    return message


def format_replay_event(event: dict[str, Any]) -> str | None:
    """Full outbound text for one replayed history event, or None to skip it."""
    etype = event.get("type")
    kind = curate(etype, event.get("payload") or {})
    if kind is None:
        return None
    if etype == "step_started":
        return step_divider(event)
    body = event_body(etype, event.get("payload") or {})
    if kind == "silent" and not body.strip():
        return None
    change = event.get("change") or {}
    handle = handle_for(str(change.get("id") or ""))
    return f"{header_line(event)}\n{format_outbound(event, handle)}"


def render_response(
    response: Response, subscribed_ids: Iterable[str] = (), alerts_on: bool = False,
) -> str:
    if isinstance(response, ListResult):
        header = "Alerts: all changes" if alerts_on else "Alerts: subscribed only"
        subscribed = set(subscribed_ids)
        visible = [
            c for c in response.changes
            if c.id in response.resident or c.id in subscribed]
        if not visible:
            return f"{header}\nNothing running."
        lines = [header]
        for c in visible:
            mark = "● " if c.id in subscribed else ""
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
        subscriptions: Subscriptions,
        alert_all: AlertAll | None = None,
    ) -> None:
        self._chat_id = chat_id
        self._submit = submit
        self._send = send
        self._get_updates = get_updates
        self._subscriptions = subscriptions
        self._alert_all = alert_all if alert_all is not None else AlertAll()
        self._offset = 0
        self._last_narration: dict[str, str] = {}
        self._msg_to_change: dict[int, str] = {}
        self._handle_to_change: dict[str, str] = {}
        # The reply maps below are WRITTEN by on_event and on_response (both run on
        # the sender thread) and READ by resolve (poll thread); _offset stays
        # poll-thread-only. Guard only the maps.
        self._lock = threading.Lock()

    @classmethod
    def from_env(
        cls,
        env: dict[str, str],
        submit: Callable[[Command], None],
        *,
        subscriptions: Subscriptions,
        alert_all: AlertAll | None = None,
        file_text: str | None = None,
    ) -> TelegramNorthAdapter:
        config = load_config(env, file_text)

        def send(text: str, silent: bool) -> int | None:
            return send_message(config.token, config.chat_id, text, silent=silent)

        get_updates = make_get_updates(config.token)

        return cls(chat_id=config.chat_id, submit=submit,
                   send=send, get_updates=get_updates, subscriptions=subscriptions,
                   alert_all=alert_all)

    def on_event(self, event: dict[str, Any]) -> None:
        """Curate one event and deliver it to chat.

        Membership is the first decision (spec §2): a subscribed Change takes the
        full curated path below (buzz + silent, deduped via Subscriptions.seen). An
        unsubscribed Change no longer returns here — it falls through to
        _alert_all_event, which delivers only buzz events and only while alert-all
        is on. A subscribed Change streams every llm_response as its own quiet
        message, so a gate (awaiting_user_input) is delivered terse — its label and
        reply handle, with no re-dump of the preceding narration. The _last_narration
        write is kept: it still feeds the alert-all path across a subscribe/
        unsubscribe toggle. That per-Change map is sender-thread-only, like _offset,
        so it needs no lock."""
        change = event.get("change") or {}
        change_id = change.get("id")
        if not change_id:
            return
        if not self._subscriptions.contains(change_id):
            self._alert_all_event(change_id, event)
            return
        event_id = event.get("id")
        if event.get("_replay"):
            if event_id is not None:
                self._subscriptions.mark_seen(change_id, event_id)
            return
        if event_id is not None and self._subscriptions.seen(change_id, event_id):
            return
        etype = event.get("type")
        kind = curate(etype, event.get("payload") or {})
        if kind is None:
            return
        if etype == "step_started":
            divider = step_divider(event)
            if divider is not None:
                self._send(divider, True)
            return
        body = event_body(etype, event.get("payload") or {})
        if etype == "llm_response" and body.strip():
            self._last_narration[change_id] = body
        if kind == "silent" and not body.strip():
            return
        handle = handle_for(change_id)
        with self._lock:
            self._handle_to_change[handle] = change_id
        full = f"{header_line(event)}\n{format_outbound(event, handle)}"
        message_id = self._send(full, kind == "silent")
        with self._lock:
            if message_id is not None:
                self._msg_to_change[message_id] = change_id

    def _alert_all_event(self, change_id: str, event: dict[str, Any]) -> None:
        """An unsubscribed Change: deliver a buzz alert loud iff alert-all is on,
        recording the same reply maps the subscribed path records and deduping on
        (change_id, event id) against the AlertAll log. Silent narrative stays
        suppressed for an unsubscribed Change. An event with no id is best-effort
        (delivered, not deduped). mark fires only on a successful send (message_id
        is not None), mirroring the map write: a send that fails without raising
        must not record the event as delivered, or the re-attach replay that
        re-surfaces this blocked Change would be permanently suppressed. A
        prompt-less awaiting_user_input falls back to this Change's preceding
        llm_response narration (recorded before the enabled gate so the first
        alert after /alerts on carries current context). An unsubscribed Change
        never streamed that narration, so the gate carries it as its only copy
        (unlike the subscribed path, which streams narration live and keeps its gate
        terse); with no prior narration it renders the 'needs your input' label
        alone, still a useful alert."""
        if event.get("_replay"):
            event_id = event.get("id")
            if event_id is not None:
                self._alert_all.mark(change_id, event_id)
            return
        etype = event.get("type")
        payload = event.get("payload") or {}
        body = event_body(etype, payload)
        if etype == "llm_response" and body.strip():
            self._last_narration[change_id] = body
        if not self._alert_all.enabled():
            return
        if curate(etype, payload) != "buzz":
            return
        event_id = event.get("id")
        if event_id is not None and self._alert_all.seen(change_id, event_id):
            return
        override: str | None = None
        if etype == "awaiting_user_input" and not body.strip():
            override = self._last_narration.get(change_id, "")
        handle = handle_for(change_id)
        with self._lock:
            self._handle_to_change[handle] = change_id
        full = f"{header_line(event)}\n{format_outbound(event, handle, override)}"
        message_id = self._send(full, False)
        with self._lock:
            if message_id is not None:
                self._msg_to_change[message_id] = change_id
        if message_id is not None and event_id is not None:
            self._alert_all.mark(change_id, event_id)

    def on_response(self, response: Response) -> None:
        text = render_response(
            response, self._subscriptions.all(), self._alert_all.enabled())
        if not text:
            return
        message_id = self._send(text, True)
        if isinstance(response, Notice) and response.change_id is not None:
            handle = handle_for(response.change_id)
            with self._lock:
                self._handle_to_change[handle] = response.change_id
                if message_id is not None:
                    self._msg_to_change[message_id] = response.change_id

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
            self._submit(parse(
                item.text, item.reply_to_message_id, self._resolve_text,
                parse_subscribe_handle))

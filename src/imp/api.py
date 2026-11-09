# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""imp's supported embedding surface: create, list and steer quests from another
process without importing imp's internals."""

from __future__ import annotations

import math
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import anyio

from imp.api_errors import (
    DaemonUnreachable,
    ImpError,
    InvalidVar,
    NotAtGate,
    QuestNotFound,
)
from imp.remote.session.attach import DaemonStartupError
from imp.remote.session.protocol import CONTROL_INTERRUPT

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path

    from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

    from imp.remote.session.client import SessionClient

EVENT_SCHEMA_VERSION = 1

__all__ = [
    "EVENT_SCHEMA_VERSION",
    "DaemonStartupError",
    "DaemonUnreachable",
    "Event",
    "ImpError",
    "InvalidVar",
    "NotAtGate",
    "QuestHandle",
    "QuestNotFound",
    "QuestSummary",
    "Session",
    "attach",
    "create_quest",
    "list_quests",
]


@dataclass(frozen=True)
class QuestHandle:
    """A quest that exists on disk, usable wherever a quest id is accepted."""

    id: str
    title: str
    control_root: Path


@dataclass(frozen=True)
class QuestSummary:
    """One quest as `list_quests` reports it, in-flight or completed."""

    id: str
    title: str
    status: str
    step: str | None


def create_quest(
    control_root: Path,
    prompt: str,
    *,
    workflow: str | None = None,
    base: str | None = None,
    title: str | None = None,
    background: bool = False,
    autonomous: bool = False,
    vars: Mapping[str, bool | int | str] | None = None,
) -> QuestHandle:
    """Mint a quest id and write its draft synchronously, in this process.
    `background=True` also starts a detached run needing no attach;
    `autonomous=True` makes it take imp's recommendations."""
    if autonomous and not background:
        raise ValueError("autonomous=True requires background=True")
    from imp.cli.main import BaseResolutionError
    from imp.cli.var_flags import VarFlagError
    from imp.create import create_draft_quest
    from imp.engine.errors import LoadError, LoadErrors
    from imp.engine.worktree import PreconditionError

    try:
        quest = create_draft_quest(
            control_root, prompt,
            workflow=workflow, base=base, title=title, vars=vars)
    except VarFlagError as e:
        raise InvalidVar(str(e)) from e
    except (BaseResolutionError, PreconditionError, LoadError, LoadErrors) as e:
        raise ImpError(str(e)) from e
    if background:
        from imp.remote.daemon import spawn
        try:
            spawn.start_quest(control_root, quest.id, autonomous=autonomous)
        except OSError as e:
            raise ImpError(
                f"quest {quest.id} was created but its run could not start: {e}") from e
    return QuestHandle(id=quest.id, title=quest.title, control_root=control_root)


def _summary(quest_id: str, status: str, step: str, title: str) -> QuestSummary:
    # Both row sources render "no step yet" as the CLI's "-" display placeholder.
    return QuestSummary(
        id=quest_id, title=title, status=status,
        step=None if step == "-" else step)


def list_quests(control_root: Path) -> list[QuestSummary]:
    """Every quest under `control_root` — live homes first, then archived ones."""
    from imp.cli.store import _row_for_id
    from imp.engine.archive import read as archive_read
    from imp.engine.errors import LoadError
    from imp.engine.quest import live_quest_ids

    # Deliberately not `_live_quest_ids`: under IMP_SESSION_SOCKET its `make_store` proxies
    # to the socket's daemon, or refuses a foreign control_root, while rows are read locally.
    live = live_quest_ids(control_root)
    out: list[QuestSummary] = []
    for quest_id in live:
        row = _row_for_id(control_root, quest_id)
        if row is None:
            continue
        out.append(_summary(row[0], row[1], row[2], row[4]))
    seen = set(live)
    try:
        rows = archive_read.completed_rows(control_root)
    except LoadError as e:
        raise ImpError(str(e)) from e
    for r in rows:
        if r[0] not in seen:
            out.append(_summary(r[0], r[1], r[2], r[4]))
    return out


# anyio's stream errors carry no message, so state the condition rather than wrap them.
_DAEMON_GONE = "lost the connection to the session daemon; it exited or hibernated"


@dataclass(frozen=True)
class Event:
    """One event off a quest's stream: its type, its payload, and the whole envelope."""

    type: str
    payload: Mapping[str, Any]
    raw: Mapping[str, Any]


class _Feed:
    """Bridges the session client's push callback to an async iterator."""

    def __init__(self) -> None:
        # Unbounded: on_event runs on the daemon's read loop and must never block or drop.
        send, recv = anyio.create_memory_object_stream[Event](max_buffer_size=math.inf)
        self._send: MemoryObjectSendStream[Event] = send
        self.recv: MemoryObjectReceiveStream[Event] = recv
        self.at_gate = False

    def on_event(self, raw: dict[str, Any]) -> None:
        # Track the gate as frames ARRIVE, not as the consumer drains them: a reply is
        # valid the moment the daemon opens the gate. Replayed history is not a gate.
        if not raw.get("_replay"):
            if raw.get("type") == "awaiting_user_input":
                self.at_gate = True
            elif raw.get("type") == "user_input_resolved":
                self.at_gate = False
        self._send.send_nowait(
            Event(type=raw.get("type", ""), payload=raw.get("payload") or {}, raw=raw))

    def close(self) -> None:
        self._send.close()   # EOF for a consumer still draining the buffer, not a cut


class Session:
    """A live attachment to one quest's session daemon."""

    def __init__(self, client: SessionClient, feed: _Feed) -> None:
        self._client = client
        self._feed = feed

    async def events(self) -> AsyncIterator[Event]:
        """Yield events until the daemon's stream ends; unknown types yield like any other."""
        try:
            async for event in self._feed.recv:
                yield event
        except anyio.ClosedResourceError:
            return   # the context exited under a concurrent reader: a clean end, not an error

    async def send_input(self, text: str) -> None:
        if not self._feed.at_gate:
            raise NotAtGate(
                "no gate is open — wait for a live awaiting_user_input event")
        try:
            await self._client.send_input(text)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError) as e:
            raise DaemonUnreachable(_DAEMON_GONE) from e

    async def interrupt(self, reason: str | None = None) -> None:
        try:
            await self._client.send_control(CONTROL_INTERRUPT, reason)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError) as e:
            raise DaemonUnreachable(_DAEMON_GONE) from e

    async def aclose(self) -> None:
        await self._client.aclose()


async def _pump(client: SessionClient, feed: _Feed) -> None:
    try:
        await client.receive_events()
    finally:
        feed.close()   # EOF for events(): the iterator ends instead of hanging


@asynccontextmanager
async def attach(
    control_root: Path, quest_id: str | QuestHandle, *, lease: bool = True,
) -> AsyncIterator[Session]:
    """Attach to a quest's session daemon, spawning it when none is resident."""
    from imp.cli.main import _resume_opens_fresh
    from imp.engine.errors import LoadError, LoadErrors
    from imp.engine.quest import Quest, live_quest_ids
    from imp.remote.session import attach as attach_mod

    ident = quest_id.id if isinstance(quest_id, QuestHandle) else quest_id
    if ident not in live_quest_ids(control_root):
        raise QuestNotFound(f"no such quest {ident!r} under {control_root}")
    try:
        quest = Quest.load(control_root, ident)
    except (LoadError, LoadErrors) as e:
        raise ImpError(str(e)) from e
    resume = not _resume_opens_fresh(quest)
    feed = _Feed()
    try:
        try:
            client = await attach_mod.attach_or_spawn(
                quest, on_event=feed.on_event, lease=lease,
                spawn_fn=lambda q: attach_mod.spawn_session_daemon(q, resume=resume))
        except OSError as e:   # incl. TimeoutError, an OSError, from the connect budget
            # fail_after's TimeoutError stringifies to "", which would otherwise leave a
            # dangling colon on the single most common failure.
            reason = str(e) or "timed out waiting for it to come up"
            raise DaemonUnreachable(
                f"could not reach the session daemon for {ident}: {reason}") from e
    except BaseException:
        feed.recv.close()
        feed.close()
        raise
    session = Session(client, feed)
    try:
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(_pump, client, feed)
                try:
                    yield session
                finally:
                    # No await between these: a pump wakeup in the gap would
                    # raise into the public API.
                    tg.cancel_scope.cancel()
                    feed.recv.close()   # the context owns the feed; leave no half open
        finally:
            # The pump has finished, so no in-flight receive races this. Closing here
            # rather than in the pump also leaves the daemon to drop the socket first
            # when the run merely ended: the end frame says the run is over, not that
            # the socket is dead, and closing first strands its still-parked reader.
            with anyio.CancelScope(shield=True):
                await client.aclose()
    except BaseExceptionGroup as eg:
        # anyio wraps the caller's own exception; a public API hands back the original.
        if len(eg.exceptions) == 1:
            raise eg.exceptions[0] from None
        raise

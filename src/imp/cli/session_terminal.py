# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Thin terminal session client: render the daemon's event stream, answer its gates.

Composes the Plan 1-7 primitives into the client half of the per-quest session
daemon. `run_terminal_client` attaches to a quest's session daemon (via an
injected attach thunk), streams the daemon's events into a live event renderer
(LiveDriver), forwards a local interrupt gesture as a control frame
(SessionControls, without killing the client), and answers each bare-turn-end
gate by soliciting one reply and sending it back as an input frame. It returns the
StreamEnding its receive loop observed: ENDED when the daemon's end-of-run frame
arrived, else CUT — the stream closed or faulted, or a deliberate leave cancelled
the loop, with no end frame seen. The CLI flip
(a later plan) builds the attach thunk, the LiveDriver, the InterruptCoordinator,
and the solicit callback, and replaces the in-process run_quest call in
`imp new` / `imp resume` with this loop.
"""
from __future__ import annotations

import contextlib
import functools
import math
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio

from imp.cli.leave import resolve_leave_action
from imp.cli.session_controls import SessionControls
from imp.cli.signals import InterruptCoordinator
from imp.cli.store import _user_message_text
from imp.cli.terminal_title import TerminalTitle
from imp.cli.terminal_watchdog import WatchedStream, terminal_gone, watch_terminal
from imp.engine.errors import LoadError, LoadErrors
from imp.engine.quest import Quest
from imp.remote.session.client import SessionClient, StreamEnding
from imp.remote.session.protocol import (
    CONTROL_INTERRUPT_LEAVE,
    INTERRUPT_EVENT_TYPES,
    SkillRequest,
)

if TYPE_CHECKING:
    from imp.cli.main import _PauseInfo

AttachFn = Callable[[Callable[[dict[str, Any]], None]], Awaitable[SessionClient]]


@dataclass(frozen=True)
class WitnessedPause:
    """A workflow_paused seen this segment. `info` is None when its reason folds to
    nothing — `awaiting_user`, or a payload carrying no reason — which is the bare
    `paused` block, not the absence of a pause."""

    info: _PauseInfo | None


class RunWitness:
    """An O(1) fold over the events this client saw — never an accumulated list. Fed
    every event, live and replayed, so a mid-run attach witnesses the whole run."""

    def __init__(self) -> None:
        self._intent: str | None = None
        self._human_turn: str | None = None
        self._paused: dict[str, Any] | None = None
        self._paused_error: dict[str, Any] | None = None
        self._pending_error: dict[str, Any] | None = None

    def note(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        payload = event.get("payload") or {}
        if etype == "user_message":
            self._note_user_message(payload)
        elif etype == "workflow_paused":
            self._paused = payload
            self._paused_error = self._pending_error
            self._pending_error = None
        elif etype == "step_started":
            self._pending_error = None
        elif etype == "error":
            self._pending_error = payload

    def _note_user_message(self, payload: dict[str, Any]) -> None:
        text = _user_message_text(payload).strip()
        if not text:
            return
        if payload.get("subtype") == "intent":
            self._intent = self._intent or text
        else:
            self._human_turn = self._human_turn or text

    def intent(self) -> str | None:
        """_resolve_intent's order: any intent-subtype message outranks the first
        genuine human turn, even one that arrived earlier."""
        return self._intent or self._human_turn

    def paused(self) -> WitnessedPause | None:
        """The outer option: None when no pause was witnessed this segment."""
        if self._paused is None:
            return None
        return WitnessedPause(info=self._folded_reason())

    def _folded_reason(self) -> _PauseInfo | None:
        from imp.cli.main import _PauseInfo
        paused = self._paused or {}
        base = paused.get("interrupt_reason") or paused.get("reason")
        if not base:
            return None
        # A your-turn park is an ordinary solicit: it has no reason to render.
        if base == "awaiting_user":
            return None
        cause: str | None = None
        error = self._paused_error
        if error is not None:
            detail = error.get("detail")
            if isinstance(detail, dict) and detail.get("last_agent_text"):
                cause = str(detail["last_agent_text"]).strip() or None
            if cause is None and error.get("message"):
                cause = str(error["message"]).strip() or None
        return _PauseInfo(base=str(base), cause=cause)


async def run_terminal_client(
    *,
    attach: AttachFn,
    driver: Any,
    coord: InterruptCoordinator,
    solicit: Callable[[str, bool], Awaitable[str | SkillRequest]],
    quest_id: str = "",
    control_root: Path | None = None,
    leave_state: dict[str, str] | None = None,
    leave_mode: str,
    title: TerminalTitle | None = None,
    stream: WatchedStream | None = None,
    provider: str | None = None,
    local_echo: Callable[[str], None] | None = None,
    on_title: Callable[[str], None] | None = None,
) -> StreamEnding:
    gate_send, gate_recv = anyio.create_memory_object_stream[tuple[str, bool]](
        max_buffer_size=math.inf)
    resolved_send, resolved_recv = anyio.create_memory_object_stream[None](
        max_buffer_size=math.inf)
    gate_state: dict[str, Any] = {"scope": None, "elsewhere": False}

    def _on_event(event: dict[str, Any]) -> None:
        if event.get("type") == "user_input_resolved":
            if not event.get("_replay"):
                resolved_send.send_nowait(None)
            return
        if event.get("type") == "provider_marker":
            # Transient daemon mirror; drive the marker directly, never through
            # driver.on_event.
            driver.set_pending_provider(
                (event.get("payload") or {}).get("provider"))
            return
        if event.get("type") == "sandbox_marker":
            # Transient daemon mirror; drive the marker directly, never through driver.on_event.
            driver.set_unsandboxed(
                bool((event.get("payload") or {}).get("unsandboxed")))
            return
        driver.on_event(event)
        replay = bool(event.get("_replay"))
        etype = event.get("type")
        if etype == "quest_renamed" and on_title is not None:
            on_title((event.get("payload") or {})["new_title"])
        if title is not None:
            if etype == "lane_acquired":
                title.set_hold_glyph((event.get("payload") or {}).get("glyph"))
            elif etype in ("lane_released", "lane_evicted"):
                title.set_hold_glyph(None)
        if etype == "interrupt_delivery_started" and not replay:
            # A replayed start belongs to an earlier gesture round.
            coord.note_interrupt_delivery_started()
        if etype == "awaiting_user_input" and not replay:
            # The run yielded to the paused prompt: the ctrl-c round is over.
            # Reset the client gesture latch so the next ctrl-c is a fresh round.
            coord.end_gesture_round()
            prompt = (event.get("payload") or {}).get("steer_hint") or ""
            interrupt = bool((event.get("payload") or {}).get("interrupt"))
            gate_send.send_nowait((prompt, interrupt))
            if title is not None:
                title.idle()
        elif etype == "quest_renamed" and not replay:
            if title is not None:
                title.update_name((event.get("payload") or {})["new_title"])
        elif (title is not None and not replay
                and etype not in INTERRUPT_EVENT_TYPES):
            title.busy()

    coord.escalation_event = anyio.Event()
    coord.set_escalation_hook(coord.escalation_event.set)

    # Arm the Ctrl-\ (SIGQUIT) trap BEFORE attaching so a deliberate detach works
    # any time, including mid-grind with no prompt up. open_signal_receiver is a
    # sync context manager that installs the loop handler on enter (replacing
    # SIGQUIT's core-dump default) and restores it on exit.
    # Only receive_events overwrites this: on ctrl-d / ctrl-\ / force-quit
    # _watch_force_quit cancels the group and it never returns — a cancelled loop
    # observed no end frame, which is exactly CUT.
    ending = StreamEnding.CUT
    sigquit = getattr(signal, "SIGQUIT", None)
    with contextlib.ExitStack() as stack:
        sigquit_signals = (
            stack.enter_context(anyio.open_signal_receiver(sigquit))
            if sigquit is not None else None)
        client = await attach(_on_event)
        stack.enter_context(
            watch_terminal(stream, functools.partial(terminal_gone, client)))
        # Snapshot should_pause AFTER attach but BEFORE binding the runner.
        # An interrupt that arrived during attach() found fn=None in signal_pause
        # and wasn't forwarded to the daemon. Detect it here so the flush task
        # can re-send CONTROL_INTERRUPT once the runner is bound.
        _interrupt_before_runner = coord.should_pause
        try:
            controls = SessionControls(client, coord)
            async with anyio.create_task_group() as tg:
                async def _watch_resolved() -> None:
                    async for _ in resolved_recv:
                        scope = gate_state["scope"]
                        if scope is not None:
                            gate_state["elsewhere"] = True
                            scope.cancel()

                async def _answer_gates() -> None:
                    try:
                        async for prompt, interrupt in gate_recv:
                            gate_state["elsewhere"] = False
                            reply: str | SkillRequest = ""
                            with anyio.CancelScope() as scope:
                                gate_state["scope"] = scope
                                try:
                                    reply = await solicit(prompt, interrupt)
                                finally:
                                    gate_state["scope"] = None
                                    # The ack belongs to this gate's gesture round, which
                                    # ends however the solicit does — including cancelled.
                                    driver.clear_stop_ack()
                            if gate_state["elsewhere"]:
                                continue     # answered elsewhere: no send_input
                            # Coalesce re-announce frames: mid-solicit the run is
                            # blocked on this reply and cannot have opened a new gate.
                            while True:
                                try:
                                    gate_recv.receive_nowait()
                                except anyio.WouldBlock:
                                    break
                            if isinstance(reply, SkillRequest):
                                cmd_line = (
                                    f"/{reply.name} {reply.args}".rstrip())
                                if local_echo is not None:
                                    local_echo(cmd_line)
                                await client.send_skill_request(
                                    reply.name, reply.args)
                                continue
                            # Echo a recognized control command before handling (non-reconciling).
                            control = reply.strip()
                            if (control in ("/abandon", "/save", "/shelve", "/clear",
                                            "/sandbox on", "/sandbox off", "/compact")
                                    or control.startswith("/model ")
                                    or control.startswith("/provider ")):
                                driver.append_committed_reply(control)
                            if reply.strip() == "/abandon":
                                if leave_state is not None:
                                    leave_state["intent"] = "abandon"
                                    if control_root is not None:
                                        try:
                                            reloaded = Quest.load(
                                                control_root, quest_id)
                                            leave_state["materialized"] = (
                                                "1" if reloaded.materialized
                                                else "")
                                        except (LoadError, LoadErrors):
                                            leave_state["materialized"] = ""
                                await controls.abandon()
                                return
                            if reply.strip() == "/save":
                                if leave_state is not None:
                                    leave_state["intent"] = "save"
                                coord.escalation_event.set()
                                return
                            if reply.strip() == "/clear":
                                if leave_state is not None:
                                    leave_state["intent"] = "clear"
                                coord.escalation_event.set()
                                return
                            if reply.strip() == "/shelve":
                                if leave_state is not None:
                                    leave_state["intent"] = "shelve"
                                coord.escalation_event.set()
                                return
                            if reply.strip().startswith("/model "):
                                # Sent as a control; stay attached, daemon applies it + re-prompts.
                                arg = reply.strip()[len("/model "):].strip()
                                await controls.set_model(arg)
                                continue
                            if reply.strip() in ("/sandbox on", "/sandbox off"):
                                arg = reply.strip()[len("/sandbox "):].strip()
                                await controls.set_sandbox(arg)
                                continue
                            if reply.strip().startswith("/provider "):
                                # Non-parking: the daemon writes the pin,
                                # broadcasts the marker and re-opens the prompt.
                                name = reply.strip()[len("/provider "):].strip()
                                await controls.set_provider(name)
                                continue
                            if reply.strip() == "/compact":
                                await controls.compact()
                                continue
                            if local_echo is not None and reply.strip():
                                local_echo(reply)
                            await client.send_input(reply)
                    except EOFError:
                        # A non-tty solicit (prompt_toolkit add_reader on a piped
                        # stdin) or Ctrl-D at a real tty surfaces as EOFError.
                        # Treat it as a deliberate detach: leave the quest paused.
                        coord.leave_reason = "ctrl-d"
                        coord.escalation_event.set()

                async def _watch_force_quit() -> None:
                    # cancel scope → finally → client.aclose; daemon detaches and runs on
                    await coord.escalation_event.wait()
                    tg.cancel_scope.cancel()

                async def _watch_sigquit() -> None:
                    # Ctrl-\ deliberate detach: same teardown as force-quit — trigger
                    # the escalation event so _watch_force_quit handles the cancel.
                    # This avoids a race in anyio's asyncio backend where the sigquit
                    # wakeup Handle lands in _ready before the socket-reader Handle,
                    # causing InvalidStateError when cancel fires before set_result.
                    if sigquit_signals is None:
                        return
                    async for _sig in sigquit_signals:
                        coord.leave_reason = "sigquit"
                        coord.escalation_event.set()
                        break

                async def _flush_pending_interrupt() -> None:
                    if _interrupt_before_runner:
                        from imp.remote.session.protocol import CONTROL_INTERRUPT
                        await client.send_control(CONTROL_INTERRUPT)

                async def _flush_pending_provider() -> None:
                    if provider is not None:
                        from imp.remote.session.protocol import CONTROL_SET_PROVIDER
                        await client.send_control(CONTROL_SET_PROVIDER, provider)

                tg.start_soon(_answer_gates)
                tg.start_soon(_watch_resolved)
                tg.start_soon(_watch_force_quit)
                tg.start_soon(_watch_sigquit)
                tg.start_soon(_flush_pending_interrupt)
                tg.start_soon(_flush_pending_provider)
                ending = await client.receive_events()
                tg.cancel_scope.cancel()
        finally:
            coord.detach_runner()
            coord.set_escalation_hook(None)
            await gate_send.aclose()
            gate_recv.close()
            await resolved_send.aclose()
            resolved_recv.close()
            driver.finish()
            if title is not None:
                title.reset()
            # Deliberate leave (ctrl-d / sigterm / sighup / Ctrl-\ / EOF / /save /
            # /shelve / /clear / headless): decide the disposition lock-free and flush a
            # control frame BEFORE aclose so the daemon observes it ahead of the
            # detach (spec "Sequencing"). A materialized/mid-run leave has no
            # disposition frame — it instead carries its trigger on a single
            # CONTROL_INTERRUPT_LEAVE so workflow_paused records the truth rather
            # than the lease-drop "detached". Abandon has its own frame path and
            # never sets the escalation event; a natural completion leaves it unset.
            if (coord.escalation_event is not None
                    and coord.escalation_event.is_set()):
                action = None
                intent = (
                    leave_state.get("intent") if leave_state is not None
                    else None)
                # A bare interrupt-pause (mid-run SIGINT with no leave gesture) trips
                # escalation via the headless detach but sets no leave_reason: it must
                # pause, not resolve a save/archive disposition. Resolve only for a
                # genuine leave (leave_reason set) or an explicit /save|/shelve|/clear.
                if (leave_state is not None and control_root is not None
                        and (intent is not None
                             or coord.leave_reason is not None)):
                    action = resolve_leave_action(
                        control_root, quest_id, leave_mode, intent)
                if action is not None and leave_state is not None:
                    _sent = False
                    with contextlib.suppress(
                        RuntimeError, anyio.BrokenResourceError,
                        anyio.ClosedResourceError,
                    ):
                        await client.send_control(action, coord.leave_reason)
                        leave_state["disposition"] = action
                        _sent = True
                    if not _sent:
                        leave_state["send_failed"] = "1"
                elif coord.leave_reason is not None:
                    # Single choke point for the leave frame; best-effort — a dead
                    # daemon must never block the leave.
                    with contextlib.suppress(
                        RuntimeError, anyio.BrokenResourceError,
                        anyio.ClosedResourceError,
                    ):
                        await client.send_control(
                            CONTROL_INTERRUPT_LEAVE, coord.leave_reason)
            await client.aclose()
    return ending

"""Daemon runner: event parsing, outbound drain, pidfile, single-daemon guard, and run_daemon."""
from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import signal
import sys
import threading
import urllib.error
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import anyio

from imp.engine.change import Change
from imp.remote.daemon import discover, spawn
from imp.remote.daemon.commands import (
    HELP_TEXT,
    ROUTE_HINT,
    Abandon,
    Command,
    Create,
    Help,
    ListChanges,
    ListResult,
    Notice,
    Replay,
    Reply,
    Response,
    Subscribe,
    Unsubscribe,
)
from imp.remote.daemon.control import ControlPlane
from imp.remote.daemon.discover import socket_is_live, socket_path
from imp.remote.daemon.focus import Focus
from imp.remote.daemon.server import SouthHub
from imp.remote.daemon.telegram_adapter import TelegramNorthAdapter, handle_for
from imp.remote.session.reply import deliver_session_reply

_log = logging.getLogger("imp.daemon")

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_LOG_NAMES = ("imp.daemon", "imp.remote")


def _install_file_logging(
    control_root: Path, env: Mapping[str, str],
) -> list[logging.Handler]:
    log_path = control_root / ".imp" / "remote-control.log"
    level = logging.DEBUG if env.get("IMP_DEBUG") == "1" else logging.INFO
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler: logging.Handler = logging.FileHandler(log_path)
    except OSError as exc:
        logging.getLogger("imp.daemon").warning(
            "remote-control file logging disabled (%s): %s", log_path, exc)
        return []
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    stderr_handler: logging.Handler = logging.StreamHandler()
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    handlers = [file_handler, stderr_handler]
    for name in _LOG_NAMES:
        logger = logging.getLogger(name)
        for handler in handlers:
            logger.addHandler(handler)
        logger.setLevel(level)
    return handlers


def _remove_file_logging(handlers: list[logging.Handler]) -> None:
    if not handlers:
        return
    for name in _LOG_NAMES:
        logger = logging.getLogger(name)
        for handler in handlers:
            logger.removeHandler(handler)
        logger.setLevel(logging.NOTSET)
    for handler in handlers:
        with contextlib.suppress(Exception):
            handler.close()


class DaemonAlreadyRunning(Exception):
    """A daemon is already accepting on this repo's socket (single daemon per repo)."""


def parse_event(line: str) -> dict[str, Any] | None:
    """One forwarded south NDJSON line → event dict, or None for non-JSON or non-object."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def drain_outbound(adapter: Any, outbound: queue.Queue[str]) -> bool:
    """Flush every queued south line into the adapter; garbage lines are dropped.
    Returns True iff at least one line was dequeued (so the sender loop can wait
    only when both queues were empty)."""
    drained = False
    while True:
        try:
            line = outbound.get_nowait()
        except queue.Empty:
            return drained
        drained = True
        event = parse_event(line)
        if event is not None:
            adapter.on_event(event)


def drain_responses(adapter: Any, responses: queue.Queue[Response]) -> bool:
    """Flush every queued command response into the adapter. Returns True iff at
    least one response was dequeued."""
    drained = False
    while True:
        try:
            response = responses.get_nowait()
        except queue.Empty:
            return drained
        drained = True
        adapter.on_response(response)


def pid_path(control_root: Path) -> Path:
    return control_root / ".imp" / "remote-control.pid"


def write_pid(control_root: Path) -> None:
    pid_path(control_root).write_text(str(os.getpid()))


def read_pid(control_root: Path) -> int | None:
    try:
        return int(pid_path(control_root).read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def remove_pid(control_root: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        pid_path(control_root).unlink()


def check_not_running(control_root: Path) -> None:
    """Raise DaemonAlreadyRunning if a daemon is already live on this repo's socket."""
    if socket_is_live(socket_path(control_root)):
        raise DaemonAlreadyRunning(
            f"a remote-control daemon is already running for {control_root}")


def _make_telegram_adapter(
    env: Mapping[str, str], submit: Callable[[Command], None], focus: Focus,
) -> TelegramNorthAdapter:
    """Default north adapter — Telegram, refusing without an allowlist."""
    return TelegramNorthAdapter.from_env(dict(env), submit, focus=focus)


_WORKER_GAP = 1.0
_SENDER_GAP = 0.05   # idle wait between outbound/response drains (latency ceiling)


def _is_transient_net_error(exc: BaseException) -> bool:
    """Connection resets, timeouts, and DNS blips on the Telegram long-poll are
    routine and self-healing — worth a one-line note, not an alarming traceback.
    A real API error (HTTPError: 401 bad token, 409 conflict) is persistent, so
    it keeps the full traceback."""
    if isinstance(exc, urllib.error.HTTPError):
        return False
    return isinstance(exc, (urllib.error.URLError, ConnectionError, TimeoutError))


class _WorkerGate:
    def __init__(self) -> None:
        self._failing = False

    def ok(self) -> None:
        self._failing = False

    def fail(self, exc: BaseException) -> None:
        if self._failing:
            _log.debug("adapter worker cycle failed: %r", exc)
            return
        self._failing = True
        if _is_transient_net_error(exc):
            _log.warning("adapter worker cycle failed (transient network error): %r", exc)
        else:
            _log.warning("adapter worker cycle failed: %r", exc, exc_info=True)


def _poll_worker(adapter: Any, stop: threading.Event) -> None:
    """Inbound only: long-poll for chat messages and submit them. Runs in its own
    thread so its blocking getUpdates long-poll never delays outbound delivery —
    that is the _sender_worker's job (a single combined loop would gate each reply
    behind the next long-poll's full timeout)."""
    gate = _WorkerGate()
    while not stop.is_set():
        try:
            adapter.poll_once()
            gate.ok()
        except Exception as exc:
            gate.fail(exc)
            stop.wait(_WORKER_GAP)


def _sender_worker(
    adapter: Any, outbound: queue.Queue[str],
    responses: queue.Queue[Response], stop: threading.Event,
) -> None:
    """Outbound only: flush south events and command responses to chat as soon as
    they appear, independent of the inbound long-poll. When both queues are empty
    it waits a short _SENDER_GAP, bounding delivery latency without busy-spinning."""
    gate = _WorkerGate()
    while not stop.is_set():
        try:
            sent = drain_outbound(adapter, outbound)
            sent = drain_responses(adapter, responses) or sent
            gate.ok()
            if not sent:
                stop.wait(_SENDER_GAP)
        except Exception as exc:
            gate.fail(exc)
            stop.wait(_WORKER_GAP)
    try:
        drain_outbound(adapter, outbound)
    except Exception:
        _log.warning("final outbound drain failed during shutdown", exc_info=True)
    try:
        drain_responses(adapter, responses)
    except Exception:
        _log.warning("final response drain failed during shutdown", exc_info=True)


def _catchup_text(
    handle: str, snap: tuple[str, str, str] | None, bodies: list[str],
) -> str:
    if snap is not None:
        status, step, title = snap
        header = f"@{handle}  {title} — {status} · {step}"
    else:
        header = f"@{handle}"
    last = bodies[-1] if bodies else ""
    return f"{header}\n{last}" if last else header


async def _handle_command(
    command: Command, control: ControlPlane, responses: queue.Queue[Response],
) -> None:
    if isinstance(command, Reply):
        await control.reply(command.change_id, command.text)
        control.focus.focus_on(command.change_id)
    elif isinstance(command, Subscribe):
        control.focus.focus_on(command.change_id)
        handle = handle_for(command.change_id)
        snap = await anyio.to_thread.run_sync(control.snapshot, command.change_id)
        bodies = await anyio.to_thread.run_sync(
            control.recent_narrative, command.change_id, 1)
        responses.put(Notice(_catchup_text(handle, snap, bodies)))
        responses.put(Notice(f"Following @{handle}."))
    elif isinstance(command, Unsubscribe):
        control.focus.clear()
        responses.put(Notice("Muted — lifecycle alerts still on."))
    elif isinstance(command, Replay):
        change_id = command.change_id or control.focus.current()
        if change_id is None:
            responses.put(Notice(ROUTE_HINT))
        else:
            bodies = await anyio.to_thread.run_sync(
                control.recent_narrative, change_id, command.n)
            if not bodies:
                responses.put(Notice(
                    f"Nothing to replay for @{handle_for(change_id)} yet."))
            else:
                for body in bodies:
                    responses.put(Notice(body))
    elif isinstance(command, Create):
        control.create(command.prompt)
        responses.put(Notice("Spawning a new Change… watch for its first question"))
    elif isinstance(command, Abandon):
        handle = handle_for(command.change_id)
        rc = await anyio.to_thread.run_sync(control.abandon, command.change_id)
        if rc == 0:
            responses.put(Notice(f"Abandoned @{handle}."))
        else:
            responses.put(Notice(
                f"@{handle} is still running — stop it first, then /abandon again."))
    elif isinstance(command, ListChanges):
        try:
            changes = await anyio.to_thread.run_sync(control.list_changes)
            responses.put(ListResult(changes))
        except Exception:
            _log.warning("list_changes failed", exc_info=True)
            responses.put(Notice("Error: could not list changes."))
    elif isinstance(command, Help):
        responses.put(Notice(HELP_TEXT))
    elif isinstance(command, Notice):
        responses.put(command)


async def _dispatch_inbound(
    inbound: queue.Queue[Command], control: ControlPlane,
    responses: queue.Queue[Response], stop: threading.Event,
) -> None:
    def _get() -> Command | None:
        while not stop.is_set():
            try:
                return inbound.get(timeout=0.05)
            except queue.Empty:
                continue
        return None

    while True:
        command = await anyio.to_thread.run_sync(_get)
        if command is None:
            return
        _log.info("routing command: %r", command)
        try:
            await _handle_command(command, control, responses)
        except Exception:
            _log.warning("command failed: %r", command, exc_info=True)


async def _await_shutdown_signals(shutdown: anyio.Event) -> None:
    """Set `shutdown` on the first SIGTERM/SIGINT (spec: a clean SIGTERM drains)."""
    with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
        async for _signum in signals:
            shutdown.set()
            return


async def deliver_to_session(
    control_root: Path, change_id: str, text: str,
) -> None:
    """Deliver one reply into change_id's session daemon, rehydrating if hibernated."""
    change = await anyio.to_thread.run_sync(Change.load, control_root, change_id)
    await deliver_session_reply(change, text)


async def run_daemon(
    control_root: Path,
    *,
    env: Mapping[str, str] | None = None,
    make_adapter: Callable[
        [Mapping[str, str], Callable[[Command], None], Focus],
        Any,
    ] = _make_telegram_adapter,
    resume: Callable[[str, str], None] | None = None,
    create: Callable[[str], None] | None = None,
    abandon: Callable[[str], int] | None = None,
    list_changes: Callable[[], list[discover.ChangeInfo]] | None = None,
    install_signals: bool = True,
    shutdown: anyio.Event | None = None,
) -> None:
    """Run the daemon until shutdown: bind socket, own adapter, relay both directions."""
    env = os.environ if env is None else env
    shutdown = anyio.Event() if shutdown is None else shutdown
    resume = resume or (lambda cid, text: spawn.resume_change(control_root, cid, text))
    create = create or (lambda prompt: spawn.create_change(control_root, prompt))
    abandon = abandon or (lambda cid: spawn.abandon_change(control_root, cid))
    list_changes = list_changes or (lambda: discover.list_changes(control_root))

    spath = socket_path(control_root)
    outbound: queue.Queue[str] = queue.Queue()
    inbound: queue.Queue[Command] = queue.Queue()
    responses: queue.Queue[Response] = queue.Queue()
    stop = threading.Event()
    focus = Focus()

    hub = SouthHub(
        str(spath), on_event=lambda cid, line: outbound.put(line),
        on_attach=lambda cid, autosubscribe: focus.focus_on(cid) if autosubscribe else None,
        on_detach=lambda cid: focus.clear_if(cid))
    control = ControlPlane(
        hub=hub, resume=resume, create=create,
        abandon=abandon, list_changes=list_changes,
        focus=focus,
        snapshot=lambda cid: discover.snapshot(control_root, cid),
        recent_narrative=lambda cid, n: discover.recent_narrative(
            control_root, cid, n))
    adapter = make_adapter(env, inbound.put, focus)

    log_handlers = _install_file_logging(control_root, env)
    _log.info("remote-control daemon starting (control_root=%s)", control_root)

    with contextlib.suppress(FileNotFoundError):
        spath.unlink()   # clear a stale socket file (guard already proved not live)
    try:
        write_pid(control_root)
        await hub.open()
        async with anyio.create_task_group() as tg:
            tg.start_soon(hub.serve)
            tg.start_soon(_dispatch_inbound, inbound, control, responses, stop)
            tg.start_soon(anyio.to_thread.run_sync, _poll_worker, adapter, stop)
            tg.start_soon(anyio.to_thread.run_sync,
                          _sender_worker, adapter, outbound, responses, stop)
            if install_signals:
                tg.start_soon(_await_shutdown_signals, shutdown)
            try:
                await shutdown.wait()
            finally:
                stop.set()
                with anyio.CancelScope(shield=True):
                    await hub.close()       # triggers ClosedResourceError in hub.serve
                    await anyio.sleep(0)    # let the hub.serve task see it before we cancel
                tg.cancel_scope.cancel()
    except Exception:
        _log.exception("remote-control daemon crashed")
        raise
    finally:
        with anyio.CancelScope(shield=True):
            await hub.close()
        with contextlib.suppress(FileNotFoundError):
            spath.unlink()
        remove_pid(control_root)
        _remove_file_logging(log_handlers)


def start_daemon(control_root: Path) -> int:
    """`imp remote-control start`: reject a second live daemon, refuse when no
    remote.driver is configured, then run in foreground."""
    from imp.cli.remote_channel import driver_config_from
    from imp.providers.effective import load_effective_config
    from imp.remote.drivers.telegram import ConfigError
    try:
        check_not_running(control_root)
    except DaemonAlreadyRunning:
        print("imp: a remote-control daemon is already running for this repo",
              file=sys.stderr)
        return 2
    config = load_effective_config(
        project_root=control_root, control_root=control_root).config
    _driver_raw = (getattr(config, "remote", None) or {}).get("driver")
    if not _driver_raw:
        print("imp: remote-control requires a configured remote.driver; add a "
              "remote.driver block to your config and restart", file=sys.stderr)
        return 2
    if driver_config_from(config) is None:
        _name = _driver_raw.get("name", "<unknown>")
        print(f"imp: remote-control: driver {_name!r} is not recognised; "
              "check remote.driver.name in your config", file=sys.stderr)
        return 2
    try:
        anyio.run(run_daemon, control_root)
    except ConfigError as exc:
        print(f"imp: remote-control not configured: {exc}", file=sys.stderr)
        return 2
    return 0


def stop_daemon(control_root: Path) -> int:
    """`imp remote-control stop`: SIGTERM the recorded daemon pid."""
    pid = read_pid(control_root)
    if pid is None:
        print("imp: no remote-control daemon running for this repo", file=sys.stderr)
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        remove_pid(control_root)
        print("imp: no remote-control daemon running for this repo", file=sys.stderr)
        return 1
    except PermissionError:
        print(f"imp: no permission to signal the daemon (pid {pid})", file=sys.stderr)
        return 1
    print(f"Sent SIGTERM to remote-control daemon (pid {pid}).")
    return 0

"""Daemon runner: event parsing, outbound drain, pidfile, single-daemon guard, and run_daemon."""
from __future__ import annotations

import contextlib
import logging
import os
import queue
import signal
import sys
import threading
import urllib.error
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import anyio

from imp.engine.archive import maintenance as archive_maintenance
from imp.engine.archive import read as archive_read
from imp.engine.change import Change
from imp.engine.errors import LoadError
from imp.remote.daemon import discover, spawn
from imp.remote.daemon.alert_all import AlertAll
from imp.remote.daemon.commands import (
    ALERTS_OFF_REPLY,
    ALERTS_ON_REPLY,
    HELP_TEXT,
    ROUTE_HINT,
    SAVE_USAGE,
    Abandon,
    Alerts,
    Command,
    Create,
    Help,
    ListChanges,
    ListResult,
    Notice,
    Replay,
    Reply,
    Response,
    Resume,
    Save,
    Search,
    Subscribe,
    Unsubscribe,
)
from imp.remote.daemon.control import ControlPlane
from imp.remote.daemon.directory import resident_session_ids
from imp.remote.daemon.discover import socket_is_live, socket_path
from imp.remote.daemon.relay_supervisor import RelaySupervisor
from imp.remote.daemon.server import SouthHub
from imp.remote.daemon.subscriptions import Subscriptions
from imp.remote.daemon.telegram_adapter import (
    TelegramNorthAdapter,
    format_replay_event,
    handle_for,
)
from imp.remote.session.reply import deliver_session_reply
from imp.remote.session.save import save_change as _save_change

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


def _drain(
    get_next: Callable[[], Any], deliver: Callable[[Any], None],
    carry: list[Any], what: str,
) -> bool:
    """Deliver the carried item (if any), then every queued item, in order.
    Returns True iff at least one item was delivered.

    `carry` is the caller-owned one-slot buffer for the in-flight item. A
    transient network failure leaves the item in `carry` so the next cycle
    retries it — an outage delays delivery instead of losing it (retrying may
    duplicate a message whose send landed but whose response read timed out;
    at-least-once beats silently-never). Any other failure is treated as poison
    for that item alone: it is dropped loudly so a persistent per-message error
    cannot wedge everything queued behind it. Both re-raise for the caller's
    gate/backoff."""
    delivered = False
    while True:
        if not carry:
            try:
                carry.append(get_next())
            except queue.Empty:
                return delivered
        try:
            deliver(carry[0])
        except Exception as exc:
            if not _is_transient_net_error(exc):
                carry.clear()
                _log.warning("dropping queued %s after non-transient send failure", what)
            raise
        carry.clear()
        delivered = True


def drain_outbound(
    adapter: Any, outbound: queue.Queue[dict[str, Any]],
    carry: list[dict[str, Any]],
) -> bool:
    """Flush the carried event, then every queued south event, into the adapter.
    Returns True iff at least one event was delivered."""
    return _drain(outbound.get_nowait, adapter.on_event, carry, "south event")


def drain_responses(
    adapter: Any, responses: queue.Queue[Response], carry: list[Response],
) -> bool:
    """Flush the carried response, then every queued command response, into the
    adapter. Returns True iff at least one response was delivered."""
    return _drain(responses.get_nowait, adapter.on_response, carry, "command response")


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
    env: Mapping[str, str], submit: Callable[[Command], None],
    subscriptions: Subscriptions, alert_all: AlertAll,
) -> TelegramNorthAdapter:
    """Default north adapter — Telegram, refusing without an allowlist."""
    return TelegramNorthAdapter.from_env(
        dict(env), submit, subscriptions=subscriptions, alert_all=alert_all)


def _make_on_attach(subscriptions: Subscriptions) -> Callable[[str, bool], None]:
    """Auto-subscribe a freshly attached Change whose hello set autosubscribe (spec
    §6). Membership only — a freshly attached Change has no prior history. No
    on_detach hook: a detach is process churn, not an intent to stop following."""
    def on_attach(change_id: str, autosubscribe: bool) -> None:
        if autosubscribe:
            subscriptions.add(change_id)
    return on_attach


_WORKER_GAP = 1.0
_SENDER_GAP = 0.05   # idle wait between outbound/response drains (latency ceiling)


def _is_transient_net_error(exc: BaseException) -> bool:
    """Connection resets, timeouts, and DNS blips on the Telegram long-poll are
    routine and self-healing — worth a one-line note, not an alarming traceback.
    So is a 5xx from Telegram's edge (502 Bad Gateway). A real API error
    (HTTPError: 401 bad token, 409 conflict) is persistent, so it keeps the
    full traceback."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
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
    adapter: Any, outbound: queue.Queue[dict[str, Any]],
    responses: queue.Queue[Response], stop: threading.Event,
) -> None:
    """Outbound only: flush south events and command responses to chat as soon as
    they appear, independent of the inbound long-poll. When both queues are empty
    it waits a short _SENDER_GAP, bounding delivery latency without busy-spinning."""
    gate = _WorkerGate()
    carry_events: list[dict[str, Any]] = []
    carry_responses: list[Response] = []
    while not stop.is_set():
        try:
            sent = drain_outbound(adapter, outbound, carry_events)
            sent = drain_responses(adapter, responses, carry_responses) or sent
            gate.ok()
            if not sent:
                stop.wait(_SENDER_GAP)
        except Exception as exc:
            gate.fail(exc)
            stop.wait(_WORKER_GAP)
    try:
        drain_outbound(adapter, outbound, carry_events)
    except Exception:
        _log.warning("final outbound drain failed during shutdown", exc_info=True)
    try:
        drain_responses(adapter, responses, carry_responses)
    except Exception:
        _log.warning("final response drain failed during shutdown", exc_info=True)



async def _handle_command(
    command: Command, control: ControlPlane, responses: queue.Queue[Response],
    alert_all: AlertAll | None = None,
) -> None:
    alert_all = alert_all if alert_all is not None else AlertAll()
    if isinstance(command, Reply):
        try:
            await control.reply(command.change_id, command.text)
        except LoadError:
            responses.put(Notice(
                f"no such change `{command.change_id}` — unknown or pruned"))
            return
    elif isinstance(command, Subscribe):
        handle = handle_for(command.change_id)
        if await anyio.to_thread.run_sync(control.snapshot, command.change_id) is None:
            responses.put(Notice(
                f"No Change @{handle} — send /show to see what's running."))
            return
        events = await anyio.to_thread.run_sync(
            control.subscriptions.snapshot_and_add,
            command.change_id,
            lambda: control.full_history(command.change_id))
        for event in events:
            text = format_replay_event(event)
            if text is not None:
                responses.put(Notice(text))
        responses.put(Notice(f"Following @{handle}."))
    elif isinstance(command, Unsubscribe):
        control.subscriptions.remove(command.change_id)
        responses.put(Notice(f"Unfollowed @{handle_for(command.change_id)}."))
    elif isinstance(command, Replay):
        change_id = command.change_id
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
            resident = await anyio.to_thread.run_sync(control.resident_ids)
            responses.put(ListResult(changes, resident))
        except Exception:
            _log.warning("list or residency probe failed", exc_info=True)
            responses.put(Notice("Error: could not list changes."))
    elif isinstance(command, Search):
        hits = await anyio.to_thread.run_sync(control.search, command.query)
        if not hits:
            responses.put(Notice(f"No archived conversations match {command.query!r}."))
        else:
            lines = [
                f"@{handle_for(cid)}  {title or '(untitled)'}  [{status or '-'}]"
                for cid, title, status, _created in hits
            ]
            responses.put(Notice("\n".join(lines)))
    elif isinstance(command, Resume):
        message = await anyio.to_thread.run_sync(control.resume, command.token)
        responses.put(Notice(message))
    elif isinstance(command, Save):
        change_id = command.change_id
        if change_id is None:
            responses.put(Notice(SAVE_USAGE))
        else:
            try:
                await anyio.to_thread.run_sync(control.save, change_id)
            except (OSError, TimeoutError):
                responses.put(Notice(
                    f"Couldn't reach the session for @{handle_for(change_id)} "
                    "to save it — it may still be running."))
            else:
                responses.put(Notice(
                    f"Saved @{handle_for(change_id)} to your Backlog."))
    elif isinstance(command, Alerts):
        if command.on is None:
            responses.put(Notice(
                ALERTS_ON_REPLY if alert_all.enabled() else ALERTS_OFF_REPLY))
        elif command.on:
            alert_all.set(True)
            responses.put(Notice(ALERTS_ON_REPLY))
        else:
            alert_all.set(False)
            responses.put(Notice(ALERTS_OFF_REPLY))
    elif isinstance(command, Help):
        responses.put(Notice(HELP_TEXT))
    elif isinstance(command, Notice):
        responses.put(command)


async def _dispatch_inbound(
    inbound: queue.Queue[Command], control: ControlPlane,
    responses: queue.Queue[Response], stop: threading.Event, alert_all: AlertAll,
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
            await _handle_command(command, control, responses, alert_all)
        except Exception:
            _log.warning("command failed: %r", command, exc_info=True)


async def _await_shutdown_signals(
    shutdown: anyio.Event,
    force_exit: Callable[[int], None] = os._exit,
) -> None:
    """Keep receiver open across signals so a second Ctrl-C force-quits cleanly."""
    with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
        draining = False
        async for _signum in signals:
            if not draining:
                draining = True
                shutdown.set()
                _log.info("shutting down — Ctrl-C again to force-quit")
            else:
                force_exit(0)


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
        [Mapping[str, str], Callable[[Command], None], Subscriptions, AlertAll],
        Any,
    ] = _make_telegram_adapter,
    deliver: Callable[[str, str], Awaitable[None]] | None = None,
    create: Callable[[str], None] | None = None,
    abandon: Callable[[str], int] | None = None,
    list_changes: Callable[[], list[discover.ChangeInfo]] | None = None,
    resident_ids: Callable[[], frozenset[str]] | None = None,
    search: Callable[[str], list[tuple[str, str, str, str]]] | None = None,
    resume: Callable[[str], str] | None = None,
    save: Callable[[str], None] | None = None,
    install_signals: bool = True,
    shutdown: anyio.Event | None = None,
) -> None:
    """Run the daemon until shutdown: bind socket, own adapter, relay both directions."""
    env = os.environ if env is None else env
    shutdown = anyio.Event() if shutdown is None else shutdown
    deliver = deliver or (lambda cid, text: deliver_to_session(control_root, cid, text))
    create = create or (lambda prompt: spawn.create_change(control_root, prompt))
    abandon = abandon or (lambda cid: spawn.abandon_change(control_root, cid))
    list_changes = list_changes or (lambda: discover.list_changes(control_root))
    resident_ids = resident_ids or (
        lambda: frozenset(resident_session_ids(control_root)))
    search = search or (lambda q: archive_read.search_archive(control_root, q))
    resume = resume or (lambda token: spawn.resume_change(control_root, token))
    save = save or (lambda cid: _save_change(control_root, cid))

    spath = socket_path(control_root)
    outbound: queue.Queue[dict[str, Any]] = queue.Queue()
    inbound: queue.Queue[Command] = queue.Queue()
    responses: queue.Queue[Response] = queue.Queue()
    stop = threading.Event()
    subscriptions = Subscriptions()
    alert_all = AlertAll()

    # on_event is a no-op: events now arrive via the supervisor's directory pull.
    # on_attach auto-subscribes a Change whose hello set autosubscribe (spec §6).
    hub = SouthHub(
        str(spath), on_event=lambda cid, line: None,
        on_attach=_make_on_attach(subscriptions))
    supervisor = RelaySupervisor(
        control_root=control_root,
        sink=lambda event: outbound.put(event),
        events=("all",),
        title_for=lambda cid: (discover.snapshot(control_root, cid) or ("", "", ""))[2],
        subscriptions=subscriptions)
    control = ControlPlane(
        deliver=deliver, create=create,
        abandon=abandon, list_changes=list_changes,
        resident_ids=resident_ids,
        subscriptions=subscriptions,
        snapshot=lambda cid: discover.snapshot(control_root, cid),
        recent_narrative=lambda cid, n: discover.recent_narrative(
            control_root, cid, n),
        search=search, resume=resume, save=save,
        full_history=lambda cid: discover.full_history(control_root, cid))
    adapter = make_adapter(env, inbound.put, subscriptions, alert_all)

    log_handlers = _install_file_logging(control_root, env)
    _log.info("remote-control daemon starting (control_root=%s)", control_root)

    with contextlib.suppress(FileNotFoundError):
        spath.unlink()   # clear a stale socket file (guard already proved not live)
    try:
        write_pid(control_root)
        reaped = archive_maintenance.reap_archived_homes(control_root)
        _log.info("reaped %d archived-terminal change home(s) at startup", reaped)
        await hub.open()
        # Daemon threads are not joined at interpreter shutdown, so a worker parked
        # in a blocking long-poll syscall is reaped at process exit rather than
        # holding the process open. stop.set() still asks them to exit cleanly at
        # the next loop boundary; they are never joined.
        poll_thread = threading.Thread(
            target=_poll_worker, args=(adapter, stop), daemon=True)
        sender_thread = threading.Thread(
            target=_sender_worker, args=(adapter, outbound, responses, stop),
            daemon=True)
        poll_thread.start()
        sender_thread.start()
        async with anyio.create_task_group() as tg:
            tg.start_soon(hub.serve)
            tg.start_soon(supervisor.run)
            tg.start_soon(_dispatch_inbound, inbound, control, responses, stop, alert_all)
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

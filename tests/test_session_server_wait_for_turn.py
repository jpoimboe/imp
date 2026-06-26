import anyio
import pytest

from imp.agents.tools.wait_for_user import AwaitingUserInput
from imp.remote.session.server import SessionServer


def _server(tmp_path=None) -> SessionServer:
    # No open() — these cases never reach the await branch, so no event loop
    # socket is needed; we drive attached_count via the socket's client set.
    sock_path = str(tmp_path / "wait.sock") if tmp_path else "/tmp/imp-test-wait.sock"
    return SessionServer(sock_path)


def test_returns_immediately_when_predicate_true() -> None:
    srv = _server()
    anyio.run(srv.wait_for_turn, lambda: True)   # returns without raising


def test_hibernates_when_no_client_and_not_head() -> None:
    srv = _server()
    srv._socket._clients = set()                 # attached_count == 0
    srv._first_attach_fired = True               # a client attached and left -> hibernate now
    with pytest.raises(AwaitingUserInput):
        anyio.run(srv.wait_for_turn, lambda: False)


def test_startup_zero_clients_waits_then_hibernates_on_timeout() -> None:
    """Never-attached + zero clients is the startup window, not a detach: wait for
    the first attach, and only hibernate (raise) once first_attach_timeout lapses."""
    srv = _server()
    srv._socket._clients = set()                 # attached_count == 0, never attached
    srv._first_attach_timeout = 0.05
    with pytest.raises(AwaitingUserInput):
        anyio.run(srv.wait_for_turn, lambda: False)


def test_polls_until_predicate_true() -> None:
    """The loop re-evaluates predicate after each wait cycle."""
    srv = _server()
    srv._socket._clients = {object()}  # simulate one attached client
    calls = 0

    def predicate() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 2  # False first call, True second

    async def _run() -> None:
        # _attach_changed is None before open(), so wait_for_attach_change returns
        # immediately; the poll_interval timeout is the loop pacing.
        await srv.wait_for_turn(predicate, poll_interval=0.01)

    anyio.run(_run)
    assert calls == 2

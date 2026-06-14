from __future__ import annotations

import queue
import threading

import pytest

from imp.remote.daemon.commands import (
    HELP_TEXT,
    Abandon,
    Create,
    Help,
    ListChanges,
    ListResult,
    Notice,
    Reply,
)
from imp.remote.daemon.discover import ChangeInfo
from imp.remote.daemon.runner import _dispatch_inbound, _handle_command


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeControl:
    def __init__(self, changes: list[ChangeInfo] | None = None) -> None:
        self.replies: list[tuple[str, str]] = []
        self.created: list[str] = []
        self.abandoned: list[str] = []
        self._changes = changes or []

    async def reply(self, change_id: str, text: str) -> str:
        self.replies.append((change_id, text))
        return "live"

    def create(self, prompt: str) -> None:
        self.created.append(prompt)

    def abandon(self, change_id: str) -> None:
        self.abandoned.append(change_id)

    def list_changes(self) -> list[ChangeInfo]:
        return self._changes


@pytest.mark.anyio
async def test_reply_calls_reply_and_enqueues_nothing() -> None:
    control = FakeControl()
    responses: queue.Queue = queue.Queue()
    await _handle_command(Reply("2026-06-13-aaaaaa", "go"), control, responses)
    assert control.replies == [("2026-06-13-aaaaaa", "go")]
    assert responses.empty()


@pytest.mark.anyio
async def test_create_calls_create_and_enqueues_a_notice() -> None:
    control = FakeControl()
    responses: queue.Queue = queue.Queue()
    await _handle_command(Create("add a foo"), control, responses)
    assert control.created == ["add a foo"]
    assert isinstance(responses.get_nowait(), Notice)


@pytest.mark.anyio
async def test_abandon_calls_abandon_and_enqueues_a_notice() -> None:
    control = FakeControl()
    responses: queue.Queue = queue.Queue()
    await _handle_command(Abandon("2026-06-13-k3f9qa"), control, responses)
    assert control.abandoned == ["2026-06-13-k3f9qa"]
    notice = responses.get_nowait()
    assert isinstance(notice, Notice) and "k3f9qa" in notice.text


@pytest.mark.anyio
async def test_listchanges_calls_list_and_enqueues_a_listresult() -> None:
    rows = [ChangeInfo(id="2026-06-13-aaaaaa", title="T", status="running", step="plan")]
    control = FakeControl(changes=rows)
    responses: queue.Queue = queue.Queue()
    await _handle_command(ListChanges(), control, responses)
    result = responses.get_nowait()
    assert isinstance(result, ListResult) and result.changes == rows


@pytest.mark.anyio
async def test_help_enqueues_the_help_text() -> None:
    responses: queue.Queue = queue.Queue()
    await _handle_command(Help(), FakeControl(), responses)
    assert responses.get_nowait() == Notice(HELP_TEXT)


@pytest.mark.anyio
async def test_notice_passes_through() -> None:
    responses: queue.Queue = queue.Queue()
    await _handle_command(Notice("hint"), FakeControl(), responses)
    assert responses.get_nowait() == Notice("hint")


class FailThenStopControl:
    """`reply` raises for the `"boom"` change, records others, and stops the loop
    once the second (good) command lands."""

    def __init__(self, stop: threading.Event) -> None:
        self.stop = stop
        self.calls: list[str] = []

    async def reply(self, change_id: str, text: str) -> str:
        self.calls.append(change_id)
        if change_id == "boom":
            raise RuntimeError("kaboom")
        self.stop.set()
        return "live"


@pytest.mark.anyio
async def test_dispatch_inbound_survives_a_failing_command(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A command that makes `_handle_command` raise must not crash the dispatch
    # loop or stall the queue: the loop logs and continues to the next command.
    stop = threading.Event()
    inbound: queue.Queue = queue.Queue()
    inbound.put(Reply("boom", "x"))
    inbound.put(Reply("2026-06-13-good", "y"))
    control = FailThenStopControl(stop)
    responses: queue.Queue = queue.Queue()

    with caplog.at_level("WARNING"):
        await _dispatch_inbound(inbound, control, responses, stop)

    # The loop processed the good command *after* the failing one — proof it
    # neither crashed nor dropped the rest of the queue.
    assert control.calls == ["boom", "2026-06-13-good"]
    assert "command failed" in caplog.text
